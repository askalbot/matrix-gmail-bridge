# Gmail Bridge
A Matrix-Gmail Puppet Bridge. 

# Documentation
Read at: https://askalbot.github.io/matrix-gmail-bridge/

# Development Setup
- Install [tmuxp](https://github.com/tmux-python/tmuxp) and [poetry](https://python-poetry.org/).
- Copy `example_dev_config.yaml` to `dev_config.yaml` and update the config. (Gmail Oauth Creds are required)
- Run `tmuxp load .` in project root. It'll start synapse and element. ( This uses [podman](https://podman.io/), but you can switch the scripts to use docker if you wish.)
- Then Open the project in vscode and press F5 and it'll start the bridge. 