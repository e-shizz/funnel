# Open-source runtime foundation

Funnel is an independent MIT-licensed project. It does not vendor or redistribute
the following runtime projects; it discovers compatible user-installed executables
and launches them as separate programs.

| Project | Role in Funnel | Upstream | License |
|---|---|---|---|
| UMU Launcher | Preferred Steam-independent launcher and runtime manager | https://github.com/Open-Wine-Components/umu-launcher | GNU GPL v3 |
| Wine | Direct compatibility-runtime fallback and the foundation used by Proton | https://www.winehq.org/ / https://gitlab.winehq.org/wine/wine | GNU LGPL v2.1 or later |
| Proton | Windows compatibility runtime selected through UMU; Steam Proton remains an optional legacy fallback | https://github.com/ValveSoftware/Proton | BSD 3-Clause for Proton's top-level code; bundled components retain their own licenses |

Funnel is not affiliated with or endorsed by Open Wine Components, WineHQ, or
Valve Corporation. Windows applications remain subject to their own licenses.
Archive, desktop-integration, GTK, and optional icon tools are installed separately
by the user and retain their respective upstream licenses.
