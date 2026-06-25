"""Packrat — download and archive free Android APKs from Google Play.

A small, modern tool inspired by the classic (now-defunct) Raccoon. It
downloads free Android APKs straight from Google Play (via the maintained
``gplaydl`` library for anonymous authentication and the Play protocol) and
keeps them in a versioned local *archive* so you can:

* grab apps without your Google account ever touching the request,
* install on devices that lack Play access,
* keep (and roll back to) old versions,
* conserve bandwidth with a local cache,
* inspect what you already have and what has a newer version upstream.

The protocol is handled by ``gplaydl``; Packrat adds the archive layer and a
friendly CLI on top.
"""

__version__ = "0.1.0"
