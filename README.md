# Gmail Bridge
A Matrix-Gmail Puppet Bridge. 

## Requirements
- A running homeserver
- A google cloud acount (billing not required)

## Gmail Setup
- Create Google Cloud project and enable Gmail Api in project
- Get Oauth credentials with scopes: 
	- https://www.googleapis.com/auth/gmail.readonly
	- https://www.googleapis.com/auth/gmail.compose
	- https://www.googleapis.com/auth/gmail.send

## Bridge Setup
- Get sample config  
```sh
docker run --rm ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main bridge_config > config.yaml
```

- Update `config.yaml` according to your project

- Generate Appservice config
```sh
docker run --rm -e GMAIL_BRIDGE_CONFIG_PATH="/config.yaml" -v $PWD/config.yaml:/config.yaml ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main hs_config > gmail_bridge.yaml
```

- Use `gmail_bridge.yaml` file in home-server config 

- Restart homeserver

- Run bridge
```sh
docker run --rm -e GMAIL_BRIDGE_CONFIG_PATH="/config.yaml" -v $PWD/config.yaml:/config.yaml ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main run_server
```

# Documentation
- [Quickstart / How To Use](./docs/quickstart.md)
- [Behaviour / Internals](./docs/behaviour.md)
