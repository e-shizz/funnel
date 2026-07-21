# Funnel

Funnel turns a Windows game into a Linux desktop launcher.

Open Funnel, drop in a game folder, ZIP, RAR, 7z archive, EXE, or installer, and wait for it to finish. Funnel finds the game, gives it an isolated Wine or Proton environment, and puts a launcher on your Desktop and in the application menu.

Your original files stay where they are.

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

Then install Funnel:

```bash
git clone https://github.com/e-shizz/funnel.git
cd funnel
./install.sh --check
./install.sh
funnel doctor
```

Open **Funnel** from the application menu and drop in one Windows game. When Funnel says **Done**, open the new launcher from your Desktop or application menu.

You can also run it from a terminal:

```bash
funnel "/path/to/game-or-archive"
```

## Linux distribution notes

Funnel has been tested on Fedora 44 with KDE Plasma.

Ubuntu, Debian, and Arch use different package names, but Funnel itself works the same way. Other distributions need equivalent packages for GTK3, PyGObject, 7-Zip, unrar, `desktop-file-utils`, and Wine or UMU.

Funnel looks for [UMU Launcher](https://github.com/Open-Wine-Components/umu-launcher) first and falls back to Wine. Steam is optional. You do not need a Steam account.

Funnel is intended for offline and DRM-free Windows games. Games that require kernel anti-cheat, proprietary launchers, or unsupported online services may not work under Wine or Proton.

## Built with Codex and GPT-5.6

Funnel was built during OpenAI Build Week with Codex and GPT-5.6. Codex helped implement the GTK drop window, safe archive extraction, game-executable selection, isolated prefixes, and generated Linux launchers. GPT-5.6 helped diagnose failures from real games and simplify the workflow around one action: drop a game in and get a launcher out.

Funnel does not call an AI model while converting a game. Conversion is local and deterministic.

## License

Funnel is licensed under the [MIT License](LICENSE). Wine, Proton, UMU Launcher, 7-Zip, unrar, and other runtime tools remain separate upstream projects. See [THIRD_PARTY.md](THIRD_PARTY.md).
