# Internals
## Puppet Mappings
- External Email <-> Matrix Virtual User
- Gmail Thread <-> Matrix Room

## Details
- Gmails when sent to matrix will have following metadata:  
```json
{
	"gmail_id": "Id_of_the_mail",
	"attachment_ids": ["Ids_of_previous_messages_that_were_attached_to_this_body"]
}
```
- Gmails with attachement will be sent in multiple messages  
	- First all the attachements one-by-one and at the last the body  
	- only the last message will have the metadata defined above
- Gmail Quotes are removed


## TO and CC
you can specify `TO/CC` for a thread by inviting all the emails and setting there power-levels.
Users with power-level `0` will be used as `TO` field of email and users with power-level `1` will
be used as `CC`.  
For example if you want to send email to `alice@example.com` as `TO` and `bob@example.com` as `CC`, then you should:

- Invite `@_gmail_bridge_alice_at_example.com` and `@_gmail_bridge_bob_at_example.com` to a room
- Set power-level for `@_gmail_bridge_alice_at_example.com` as `0` and power-level for `@_gmail_bridge_bob_at_example.com` as `-1`.  

Same can be done already existing thread where you can invite virtual users or change there power-levels to
specify `TO/CC` for future emails.


