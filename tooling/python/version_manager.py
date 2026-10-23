from activation import get_edition

EDITIONS = {
    "standard": {
        "name": "LocalAI Standard",
        "cloud": False,
        "advanced": False,
    },
    "pro": {
        "name": "LocalAI Pro",
        "cloud": True,
        "advanced": True,
    },
    "ultra": {
        "name": "LocalAI Ultra",
        "cloud": True,
        "advanced": True,
        "experimental": True,
    },
}


def activate(code):
    return get_edition(code)


def features(edition):
    return EDITIONS.get(edition, EDITIONS["standard"])
