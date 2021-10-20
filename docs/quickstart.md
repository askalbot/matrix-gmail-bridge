# Login
<!-- TODO: support encryption for auth room -->
- Create a new room with encryption disabled (This room will be referred as `auth-room` in rest of document)
- invite appservice to the room
    - `@gmail:example.com` (based on default config)
- appservice will join the room and send instructions
- send msg `start` to start the oauth flow.
- Go to link sent by appservice, allow all scopes and copy token.
- Send token in room. Appservice will send an confirmation message if oauth was successful.


# Config
There are two configuration options available to each user.
You can check current config status by sending `status` in `auth-room`  
  
### Email Alias
Send `email emailYouWantToUse@example.com` (in `auth-room`) to use `emailYouWantToUse@example.com` for sending mails.  
This email address should be configured as alias in your gmail account. Otherwise gmail will fallback to default email address.
  
### Name
Send `name my name` (in `auth-room`) to use `my name` as display name for mails.




# Starting a new thread
- Create a new room with encryption disabled
- Invite virtual user to room to whom the email should be sent.
    - To send email to `myemail@gmail.com`, you should invite `@_gmail_bridge_myemail_at_gmail.com:example.com` (based on default config)
    - Refer to [Behaviour Docs](./behaviour.md) for more details
- If the email is correct and bridge is correctly running then the invitation will be accepted instantly.
- Send the message in matrix room.
    - This will create a new thread in gmail.

# Interaction with External Thread
On every new thread appservice will invite you to a new room representing that particular thread.
You can join the room and send messages in room. 