"""Production-grade parser for PubMed efetch XML.

PubMed returns article records as XML: one <PubmedArticleSet> containing many
<PubmedArticle> elements. This module turns that XML into clean Python objects
with the fields most people need: title, abstract, authors, MeSH concepts and
key metadata such as journal, year and DOI.

Design goals:
    * Streaming: memory use stays flat even for files with 30,000+ articles.
    * Fault tolerant: one malformed article is logged and skipped, not fatal.
    * Safe: uses defusedxml, so malicious XML (entity bombs, XXE) is rejected.

Usage:
    from pubmed_parser import PubMedParser

    parser = PubMedParser()
    for article in parser.parse("pubmed.xml"):
        print(article.pmid, article.title)
    print(parser.parsed, parser.skipped)
"""
from __future__ import annotations
import structlog
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Iterator, Optional, Union
from xml.etree.ElementTree import Element
from defusedxml.ElementTree import iterparse  # safe against XML bombs / XXE

# Library code only gets a logger. Logging is configured once by the
# application entry point (setup_production_logging), never here.
logger = structlog.get_logger(__name__)

# Anything parse_pubmed can read from: a path string, a Path, or an open
# binary file object (useful for uploads and tests).
Source = Union[str, Path, IO[bytes]]


# ---------------------------------------------------------------------------
# 1. Data model: a fixed, typed shape for every article
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Author:
    """One author of an article.

    Attributes:
        name: Display name, e.g. "Jane Smith". For group authors this is the
            group name, e.g. "SWISS Investigators".
        last_name: Family name. None for group authors.
        fore_name: Given name(s). None for group authors or if not provided.
        affiliations: Institutions listed for this author. Often only the
            first author has one, so this list may be empty.
    """

    name: str
    last_name: Optional[str] = None
    fore_name: Optional[str] = None
    affiliations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MeshTerm:
    """One MeSH (Medical Subject Headings) concept attached to an article.

    MeSH is the controlled vocabulary NLM indexers use to describe what an
    article is about. Each term is a descriptor (the main concept) optionally
    narrowed by qualifiers (the aspect discussed).

    Example: descriptor "Brain Ischemia" with qualifiers ["genetics"] means
    the article discusses the genetics of brain ischemia.

    Attributes:
        descriptor: The concept name, e.g. "Brain Ischemia".
        ui: The stable MeSH identifier, e.g. "D002545". Prefer this over the
            name when joining with other datasets, since names can change.
        major_topic: True if this concept is a main focus of the article
            (on the descriptor or on any of its qualifiers).
        qualifiers: Aspect names such as "genetics" or "complications".
    """

    descriptor: str
    ui: Optional[str]
    major_topic: bool
    qualifiers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PubMedArticle:
    """A single parsed PubMed record.

    Every field is always present. Values that are missing in the source XML
    come through as None (for single values) or an empty list (for lists),
    so callers never need to check whether a key exists.

    The object is frozen (read only) so a parsed record cannot be changed by
    accident further down the pipeline.

    Attributes:
        pmid: PubMed ID, the unique key for the article. Always present.
        title: Article title with inline formatting (italics etc.) removed.
        abstract: Full abstract text. Structured abstracts keep their section
            labels, one section per line, e.g. "METHODS: ...".
        authors: Authors in the order listed in the article.
        journal: Full journal name, e.g. "PloS one".
        pub_year: Publication year as an integer, e.g. 2013.
        doi: Digital Object Identifier, e.g. "10.1371/journal.pone.0075035".
        publication_types: e.g. ["Journal Article", "Case Reports"].
        mesh_terms: All MeSH concepts with full detail.
        chemicals: Names of substances indexed for the article.
        keywords: Author-supplied keywords. Often empty for older articles.
    """

    pmid: str
    title: Optional[str]
    abstract: Optional[str]
    authors: list[Author]
    journal: Optional[str]
    pub_year: Optional[int]
    doi: Optional[str]
    publication_types: list[str]
    mesh_terms: list[MeshTerm]
    chemicals: list[str]
    keywords: list[str]

    @property
    def concepts(self) -> list[str]:
        """Names of the MeSH terms marked as a major topic.

        This is usually the cleanest short list of what an article is about,
        which makes it a good field for search filters or RAG metadata.
        """
        return [m.descriptor for m in self.mesh_terms if m.major_topic]

    def to_dict(self) -> dict:
        """Convert the article to a plain dict, ready for json.dump or a DataFrame.

        Nested authors and MeSH terms become nested dicts, and the computed
        `concepts` list is included as a regular key.
        """
        d = asdict(self)
        d["concepts"] = self.concepts
        return d


class PubMedParseError(Exception):
    """Raised when the input cannot be parsed.

    Covers a missing or unreadable file, broken XML, XML rejected for
    security reasons, and (in strict mode only) a malformed article.
    The original exception is attached as __cause__ for debugging.
    """


# ---------------------------------------------------------------------------
# 2. The parser: public API, field extractors and low-level helpers
# ---------------------------------------------------------------------------
class PubMedParser:
    """Streams PubMed XML into PubMedArticle objects and keeps running counts.

    Create one parser and reuse it across many files or API batches. The
    counts accumulate across every call to parse(), so after a batch job you
    can read the totals directly instead of digging through logs.

        parser = PubMedParser()
        for batch_xml in batches:
            for article in parser.parse(batch_xml):
                save(article)
        print(parser.parsed, parser.skipped)

    Each call to parse() also logs its own counts plus the running totals.

    The class is organised in three layers:
        * Public API: parse(), parse_article(), reset(), total.
        * Field extractors (_abstract, _authors, _mesh, _doi): one field each.
          To change how a field is read, subclass and override just that one.
        * Low-level helpers (_text, _texts, _year): safe XML reads. These are
          static methods because they need nothing from the parser itself.

    Attributes:
        strict: If False (default), a malformed article is logged and skipped.
            If True, the first malformed article raises PubMedParseError.
        parsed: Total articles successfully parsed across all parse() calls.
        skipped: Total malformed articles skipped across all parse() calls.
    """

    def __init__(self, *, strict: bool = False) -> None:
        """Create a parser.

        Args:
            strict: See the class docstring. Keyword only, so call it as
                PubMedParser(strict=True).
        """
        self.strict = strict
        self.parsed = 0
        self.skipped = 0

    # -- public API ---------------------------------------------------------
    @property
    def total(self) -> int:
        """Number of articles seen so far, parsed plus skipped."""
        return self.parsed + self.skipped

    def reset(self) -> None:
        """Set the parsed and skipped counts back to zero."""
        self.parsed = 0
        self.skipped = 0

    def parse(self, source: Source) -> Iterator[PubMedArticle]:
        """Read PubMed XML and yield one parsed article at a time.

        This is a generator: articles are parsed lazily as you loop, and each
        one is freed from memory once yielded, so large files use little memory.

        A record is counted as parsed as soon as it is built, before it is
        handed to you, so the counts stay correct even if you stop the loop
        early with break.

        Args:
            source: Path to the XML file (str or Path), or an open binary
                file object. For XML held in memory, such as an API response,
                pass io.BytesIO(response.content).

        Yields:
            PubMedArticle objects, in the order they appear in the source.

        Raises:
            PubMedParseError: If the source cannot be read, the XML is broken
                or unsafe, or (in strict mode) an article is malformed.
        """
        run_parsed = run_skipped = 0
        try:
            for _, el in iterparse(source, events=("end",)):
                if el.tag != "PubmedArticle":
                    continue
                try:
                    article = self.parse_article(el)
                except Exception as exc:
                    if self.strict:
                        raise PubMedParseError(str(exc)) from exc
                    run_skipped += 1
                    self.skipped += 1
                    logger.warning("pubmed_article_skipped", error=str(exc))
                else:
                    run_parsed += 1
                    self.parsed += 1
                    yield article
                finally:
                    el.clear()  # free memory for the processed article
        except PubMedParseError:
            raise
        except Exception as exc:  # broken XML, missing file, blocked entity...
            raise PubMedParseError(f"Cannot parse {source!r}: {exc}") from exc
        finally:
            logger.info(
                "pubmed_parse_done",
                parsed=run_parsed,
                skipped=run_skipped,
                total_parsed=self.parsed,
                total_skipped=self.skipped,
            )

    def parse_article(self, pa: Element) -> PubMedArticle:
        """Convert one <PubmedArticle> XML element into a PubMedArticle object.

        This is the single-record building block used by parse(). It can also
        be called directly if you already have the element. It does not
        change the parsed or skipped counts; only parse() does that.

        Args:
            pa: A <PubmedArticle> element.

        Returns:
            The parsed article.

        Raises:
            ValueError: If the record has no PMID or no <Article> section. Such
                a record cannot be identified, so it is rejected instead of
                producing a half-empty object.
        """
        mc = pa.find("MedlineCitation")
        pmid = mc.findtext("PMID") if mc is not None else None
        if not pmid:
            raise ValueError("article has no PMID")
        art = mc.find("Article")
        if art is None:
            raise ValueError(f"PMID {pmid}: missing <Article>")

        return PubMedArticle(
            pmid=pmid.strip(),
            title=self._text(art.find("ArticleTitle")),
            abstract=self._abstract(art),
            authors=self._authors(art),
            journal=art.findtext("Journal/Title"),
            pub_year=self._year(art.find("Journal/JournalIssue")),
            doi=self._doi(pa, art),
            publication_types=self._texts(art, "PublicationTypeList/PublicationType"),
            mesh_terms=self._mesh(mc),
            chemicals=self._texts(mc, "ChemicalList/Chemical/NameOfSubstance"),
            keywords=self._texts(mc, "KeywordList/Keyword"),
        )

    # -- field extractors: one job each, override in a subclass to customise --
    def _abstract(self, art: Element) -> Optional[str]:
        """Build the abstract text from an <Article> element.

        Abstracts may be a single block of text or several labelled sections
        (BACKGROUND, METHODS, RESULTS, CONCLUSIONS). Labelled sections are
        returned as "LABEL: text", one per line, so the structure is kept.

        Args:
            art: The <Article> element.

        Returns:
            The abstract as one string, or None if the article has no abstract.
        """
        parts = []
        for at in art.findall("Abstract/AbstractText"):
            body = self._text(at)
            if body:
                label = at.get("Label")
                parts.append(f"{label}: {body}" if label else body)
        return "\n".join(parts) or None

    def _authors(self, art: Element) -> list[Author]:
        """Extract the author list from an <Article> element.

        Handles two kinds of author entry:
            * Individual authors with <ForeName> and <LastName>.
            * Group authors with <CollectiveName>, e.g. a study consortium.
        Entries with no usable name are skipped.

        Args:
            art: The <Article> element.

        Returns:
            Authors in the order they appear. Empty if there is no author list.
        """
        out = []
        for a in art.findall("AuthorList/Author"):
            collective = self._text(a.find("CollectiveName"))
            if collective:
                out.append(Author(name=collective))
                continue
            fore, last = a.findtext("ForeName"), a.findtext("LastName")
            name = " ".join(p for p in (fore, last) if p)
            if name:
                out.append(Author(name, last, fore, self._texts(a, "AffiliationInfo/Affiliation")))
        return out

    def _mesh(self, mc: Element) -> list[MeshTerm]:
        """Extract MeSH concepts from a <MedlineCitation> element.

        A term counts as a major topic if either the descriptor or any of its
        qualifiers is flagged MajorTopicYN="Y". Headings without a descriptor
        name are skipped.

        Args:
            mc: The <MedlineCitation> element.

        Returns:
            MeSH terms in document order. Empty for articles not yet indexed,
            which is common for very recent publications.
        """
        out = []
        for mh in mc.findall("MeshHeadingList/MeshHeading"):
            d = mh.find("DescriptorName")
            if d is None or not self._text(d):
                continue
            quals = mh.findall("QualifierName")
            major = d.get("MajorTopicYN") == "Y" or any(q.get("MajorTopicYN") == "Y" for q in quals)
            out.append(MeshTerm(
                self._text(d), d.get("UI"), major,
                [self._text(q) for q in quals if self._text(q)],
            ))
        return out

    def _doi(self, pa: Element, art: Element) -> Optional[str]:
        """Find the article's DOI.

        The DOI can appear in two places, and some records only have it in one:
            1. <PubmedData><ArticleIdList><ArticleId IdType="doi"> (checked first)
            2. <Article><ELocationID EIdType="doi"> (fallback)

        Args:
            pa: The <PubmedArticle> element.
            art: The <Article> element inside it.

        Returns:
            The DOI string, or None if neither location has one.
        """
        for el in pa.findall("PubmedData/ArticleIdList/ArticleId"):
            if el.get("IdType") == "doi" and el.text:
                return el.text.strip()
        for el in art.findall("ELocationID"):
            if el.get("EIdType") == "doi" and el.text:
                return el.text.strip()
        return None

    # -- low-level helpers: need nothing from the parser, so static ---------
    @staticmethod
    def _text(el: Optional[Element]) -> Optional[str]:
        """Return all text inside an XML element as one clean string.

        Unlike `el.text`, this also collects text inside nested tags, so
        "<ArticleTitle>Role of <i>NOTCH3</i> in CADASIL</ArticleTitle>" gives
        "Role of NOTCH3 in CADASIL" instead of being cut off at "Role of ".
        Line breaks and repeated spaces are collapsed into single spaces.

        Args:
            el: The element to read. May be None.

        Returns:
            The cleaned text, or None if the element is missing or empty.
        """
        if el is None:
            return None
        value = " ".join("".join(el.itertext()).split())
        return value or None

    @staticmethod
    def _texts(parent: Element, path: str) -> list[str]:
        """Return the cleaned text of every element matching a path.

        Args:
            parent: Element to search under.
            path: ElementTree path such as "KeywordList/Keyword".

        Returns:
            List of non-empty strings, in document order. Empty if none match.
        """
        return [t for t in (PubMedParser._text(e) for e in parent.findall(path)) if t]

    @staticmethod
    def _year(issue: Optional[Element]) -> Optional[int]:
        """Extract the publication year from a <JournalIssue> element.

        PubMed stores the date in one of two ways:
            * <PubDate><Year>2013</Year>...</PubDate>
            * <PubDate><MedlineDate>2013 Sep-Oct</MedlineDate></PubDate>
        Both are handled by taking the first four characters.

        Args:
            issue: The <JournalIssue> element. May be None.

        Returns:
            The year as an int, or None if no valid year is found.
        """
        if issue is None:
            return None
        raw = issue.findtext("PubDate/Year") or issue.findtext("PubDate/MedlineDate") or ""
        digits = raw.strip()[:4]
        return int(digits) if digits.isdigit() else None


# ---------------------------------------------------------------------------
# 3. Convenience shortcut
# ---------------------------------------------------------------------------
def parse_pubmed(source: Source, *, strict: bool = False) -> Iterator[PubMedArticle]:
    """Shortcut for a one-off parse when you don't need the counts in code.

    Equivalent to PubMedParser(strict=strict).parse(source). Counts are still
    logged, but not kept anywhere after the call.
    """
    return PubMedParser(strict=strict).parse(source)
