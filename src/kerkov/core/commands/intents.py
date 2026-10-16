"""The action each slash command stands for, independent of any front end."""

from enum import Enum


class Intent(Enum):
    """What a parsed line asks the session to do."""

    CHAT = "chat"  # not a command — an ordinary message for the model
    HELP = "help"
    PROMPT = "prompt"
    CLEAR = "clear"
    QUIT = "quit"
    MODE = "mode"
    AUTONOMY = "autonomy"
    VERBOSITY = "verbosity"
    APPROVAL = "approval"
    YOLO = "yolo"
    PROVIDER = "provider"
    MODEL = "model"
    SKILL = "skill"
    ASSET = "asset"
    TARGET = "target"
    AGENTS = "agents"
    BACK = "back"
    ENGAGEMENTS = "engagements"
    USAGE = "usage"
    RESUME = "resume"
    ZAP = "zap"
    SEARXNG = "searxng"
    AUTH = "auth"
    LOGOUT = "logout"
