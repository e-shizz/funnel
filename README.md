# Funnel

Funnel turns a Windows program into a normal Linux desktop app.

Open Funnel, drop in a Windows folder, ZIP, RAR, 7z archive, EXE, installer, or full-trust MSIX, and wait for it to finish. Funnel adds the result to your application menu and puts a launcher on your Desktop. The original file or folder stays where it is and is never rewritten.

Funnel uses UMU or Wine to run the program. Steam is optional and no Steam account is needed.

## Install

Install the dependencies for your distribution:

```bash
# Fedora
sudo dnf install git 7zip unrar wine desktop-file-utils python3-gobject gtk3

# Ubuntu or Debian
sudo apt install git p7zip-full unrar wine desktop-file-utils python3-gi gir1.2-gtk-3.0

# Arch Linux
sudo pacman -S git 7zip unrar wine desktop-file-utils python-gobject gtk3
```

Clone and install Funnel:

```bash
git clone https://github.com/e-shizz/funnel.git
cd funnel
./install.sh --check
./install.sh
funnel doctor
```

The installer does not use sudo. It installs Funnel under `~/.local` and adds it to the application menu.

## Use

Open **Funnel** from your application menu and drop one Windows item onto the window. When Funnel says it is done, open the new launcher from your Desktop or application menu.

You can also run it from a terminal:

```bash
funnel "/path/to/program.zip"
```

## Linux distribution notes

Funnel has been tested on Fedora 44 with KDE Plasma. Ubuntu, Debian, and Arch use the same application code; only their dependency package names are different. Those distributions have not received the same clean-install testing yet.

Your desktop environment must support normal FreeDesktop application launchers. Funnel prefers [UMU Launcher](https://github.com/Open-Wine-Components/umu-launcher) when it is installed, then falls back to system Wine. The first UMU launch may download its Proton runtime.

Windows compatibility still depends on Wine or Proton. Some Windows programs, drivers, and Microsoft services will not work on Linux.

## Built with Codex and GPT-5.6

Funnel was built during OpenAI Build Week with Codex and GPT-5.6. Codex helped implement the GTK drop window, archive handling, executable selection, installer discovery, MSIX support, and Linux launchers. GPT-5.6 was also used to inspect failures and refine the final workflow while Ethan tested it with real Windows programs.

Funnel itself is local software. It does not call an AI model or online API when converting an application.

## License

Funnel is released under the MIT License. UMU, Wine, Proton, and the other tools it uses remain separate projects with their own licenses. See [THIRD_PARTY.md](THIRD_PARTY.md) for links.
