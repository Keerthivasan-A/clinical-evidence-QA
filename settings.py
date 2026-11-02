"""Project settings: load secrets and connection strings from the .env file.

Nothing sensitive is hard-coded in the code base. Copy `.env.example` to `.env`
(which git ignores), fill in your values, and call `load_env()` once at each
entry point. Variables already set in the shell win over the file.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> bool:
    """Load variables from the .env file into the process environment.

    Args:
        path: The .env file to read. Defaults to .env in the project root,
            whatever the current working directory is.

    Returns:
        True if the file was found and read, False if it does not exist.
    """
    return load_dotenv(path, override=False)


def get_required(name: str) -> str:
    """Read a required environment variable.

    Args:
        name: Variable name, e.g. "DATABASE_URL".

    Returns:
        The variable's value.

    Raises:
        RuntimeError: If the variable is missing or empty, with a hint on
            how to set it. The value itself is never put in the message.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env, fill it in, and run again.")
    return value
