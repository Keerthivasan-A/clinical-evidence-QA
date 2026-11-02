import pandas as pd
from pathlib import Path
import structlog
from logs.logger_config import setup_production_logging

# Run this once during application initialization
setup_production_logging()

# Get the logger instance for this module
logger = structlog.get_logger(__name__)

# Init
INPUT_FILE_NAME = "BioASQ_extracted.xlsx"
OUTPUT_FILE_NAME = "pmdids_to_fetct.txt"
FILE_PATH = Path(__file__).parent.parent / "data"
INPUT_FILE_PATH = FILE_PATH / INPUT_FILE_NAME
OUTPUT_FILE_PATH = FILE_PATH / OUTPUT_FILE_NAME
PUBMED_ID_COLUMN = "pubmed_ids"
N_SAMPLES_PER_GROUP = 2

# If the input folder doesn't exist quit
if not Path(INPUT_FILE_PATH).is_file():
    logger.error(f"File not found: {INPUT_FILE_PATH}")
    raise FileNotFoundError(f"File not found: {INPUT_FILE_PATH}")

# Get N samples
df_sampled = (pd.read_excel(INPUT_FILE_PATH)
              .groupby("type", group_keys=False)
              .apply(lambda x: x.sample(n=N_SAMPLES_PER_GROUP, random_state=42))
              )

# Convert all pubmed ids to a proper string
df_sampled[PUBMED_ID_COLUMN] = df_sampled[PUBMED_ID_COLUMN].apply(lambda x: list(str(x).strip() for x in x.split(",")))

# Get unique pubmed_ids and store it to text file
_ = (df_sampled
     .explode([PUBMED_ID_COLUMN])
     .select(PUBMED_ID_COLUMN)
     .drop_duplicates()
     .to_csv(OUTPUT_FILE_PATH, index=False, header=False))

logger.info(f"successfully extracted pubmed ids to {OUTPUT_FILE_PATH}")
