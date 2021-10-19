# Puppet Mappings
- External Email <-> Matrix Virtual User
- Gmail Thread <-> Matrix Room

## Details
- gmails when sent to matrix will have following metadata:  
	`{"gmail_id": "Id_of_the_mail", "attachment_ids": ["Ids_of_previous_messages_that_were_attached_to_this_body"]}`
- gmails with attachement will be sent in multiple messages  
	- First all the attachements one-by-one and at the last the body  
	- only the last message will have the metadata defined above
- Gmail Quotes are removed