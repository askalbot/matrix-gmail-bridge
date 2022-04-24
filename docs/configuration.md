# Configuration Options

## AS_TOKEN
Token that'll be used by homeserver to authenticate appservice. This of this as a password. It should be a randomly generated string.

## HS_TOKEN
Token that'll be used by bridge to authenticate homeserver. This will be used to verify that events that bridge recieves are coming from the homeserver. It should be a randomly generated string.
	
## BRIDGE_ID
ID for this matrix bridge",
> default=gmail

## BRIDGE_URL
url(without port) by which homeserver can access the bridge
>default=http://localhost

## PORT
port to start the bridge on
>default=8010

## SENDER_LOCALPART
Bridge Localpart
>default=appservice-gmail

## NAMESPACE_PREFIX
prefix to use for room aliases and users created by this bridge
> default=_gmail_bridge_


## HOMESERVER_URL
url by which bridge can access homeserver

## HOMESERVER_NAME
Name of homeserver

## HOST
hostname to use for starting bridge webserver
> default=localhost

## GMAIL_CLIENT_ID
Gmail Client Id from Oauth credentials from google cloud project.

## GMAIL_CLIENT_SECRET
Gmail Client Secret from Oauth credentials from google cloud project.

## DEFAULT_EMAIL_NAME (Optional)
default email display name to use for sending mails.

## GMAIL_RECHECK_SECONDS
Interval between polling gmail api for new mails. (default is every 5 minutes)
>default=300