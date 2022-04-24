# Quick Start

In this guide we'll setup the bridge and go through basic usage.

## Requirements
- A running homeserver (that you can manage, specifically restart)
- A google cloud acount (billing not required)

## Gmail Setup
- Create Google Cloud project and enable `Gmail Api` and `People Api` in the project.

- Create Oauth credentials with following scopes: 
	- https://www.googleapis.com/auth/gmail.readonly
	- https://www.googleapis.com/auth/gmail.compose
	- https://www.googleapis.com/auth/gmail.send
	- https://www.googleapis.com/auth/userinfo.email
	- https://www.googleapis.com/auth/userinfo.profile


## Bridge Setup
- Get sample config file:
```sh
docker run --rm ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main bridge_config > config.yaml
```

- Update `config.yaml` according to your project.

- Generate Appservice config (this will be used by homeserver):
```sh
docker run --rm -e GMAIL_BRIDGE_CONFIG_PATH="/config.yaml" -v $PWD/config.yaml:/config.yaml ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main hs_config > gmail_bridge.yaml
```

- Add `gmail_bridge.yaml` file to homeserver config.
- Restart homeserver.
- Start bridge:
```bash
docker run --rm -e GMAIL_BRIDGE_CONFIG_PATH="/config.yaml" -v $PWD/config.yaml:/config.yaml ghcr.io/askalbot/matrix-gmail-bridge:main python3 -m app.main run_server
```

## Login
- Create a new room with encryption disabled (This room will be referred as `dm` in rest of document)
- invite appservice to the room
    - `@gmail:example.com` (based on default config)
- appservice will join the room and send instructions.
- send msg `start` to start the oauth flow.
- Go to link sent by appservice, allow all scopes and copy token.
- Send token in room. Appservice will send an confirmation message if oauth was successful.


You can check current status by sending `status` in `dm`  
## User Config
There are two configuration options available to each user.
  
### Name
Send `name Alice` (in `dm`) to use `Alice` as display name for mails. Name can contain spaces.

### Email Alias
> Not yet tested

Send `email emailYouWantToUse@example.com` (in `dm`) to use `emailYouWantToUse@example.com` for sending mails.  
This email address should be configured as alias in your gmail account. Otherwise gmail will fallback to default email address.
  
## Starting a new thread
- Create a new room with encryption disabled
- Invite virtual user to room to whom the email should be sent.
    - To send email to `myemail@gmail.com`, you should invite `@_gmail_bridge_myemail_at_gmail.com:example.com` (based on default config)
    - Refer to [Behaviour Docs](./behaviour.md) for more details
- If the email is correct and bridge is correctly running then the invitation will be accepted instantly.
- Send the message in matrix room.
    - This will create a new thread in gmail.

## Interaction with External Thread
On every new thread appservice will invite you to a new room representing that particular thread.
You can join the room and send messages in room. 