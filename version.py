"""Single source of truth for the app's version number. Bump this (and
only this) when cutting a new release -- setup_installer.py's installed
DisplayVersion and the in-app update checker (updater.py) both read from
here, so they can never drift out of sync with each other."""

VERSION = "1.3.0"
