"""RSSHub Cookie Sync server implementation (standard library only).

``rsshub_cookie_sync.py`` next to this package is the executable entry point.
Module layout, from the bottom of the dependency graph upwards:

* ``constants``, ``errors``, ``clock`` -- shared vocabulary
* ``fsutil``, ``validation``, ``envfile``, ``state`` -- files, input checks, state
* ``config`` -- runtime configuration
* ``transport``, ``probes``, ``docker``, ``notify`` -- external systems
* ``compose_text``, ``compose_migration`` -- Compose file migration
* ``service`` -- the apply/monitor state machine
* ``deployment``, ``cli`` -- installer helpers and the command line
"""
