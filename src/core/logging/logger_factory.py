import logging
from pathlib import Path
from datetime import datetime

DEFAULT_LOGGING_DIR = "logs"

def get_logger(
    class_name: str, 
    log_level: int = logging.INFO,
    alsoPrintToConsole: bool = False,
) -> logging.Logger:
    Path(DEFAULT_LOGGING_DIR).mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"{DEFAULT_LOGGING_DIR}/kerkov_{timestamp}.log"

    handlers = [logging.FileHandler(log_file)]
    if alsoPrintToConsole:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    return logging.getLogger(class_name)
