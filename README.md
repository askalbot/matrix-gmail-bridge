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
TODO


## Details
- gmails when sent to matrix will have following metadata:  
	`{"gmail_id": "Id_of_the_mail", "attachment_ids": ["Ids_of_previous_messages_that_were_attached_to_this_body"]}`
- gmails with attachement will be sent in multiple messages  
	- First all the attachements one-by-one and at the last the body  
	- only the last message will have the metadata defined above
- Gmail Quotes are removed

