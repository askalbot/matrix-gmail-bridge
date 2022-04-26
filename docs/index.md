# Matrix Gmail Bridge

This is an implementation of simple puppeted matrix bridge for gmail.

## Bridging Limitation
### Simple Puppet
Right now it's a [simple puppeted bridge](https://matrix.org/docs/guides/types-of-bridging#simple-puppeted-bridge) so it can be used to send/recv emails through matrix, but it won't be able to reflect mails sent using gmail platform.  

Assuming Bob is using bridge to interact with gmail:

- Bob send a mail to Alice using matrix: visible on matrix to Bob
- Bob recieves a mail from Alice: visible on matrix to Bob
- Bob send a mail to Alice using gmail (web/app): **NOT** visible on matrix to Bob

### Partial History
Bridge does not sync all the activities from a thread. Only the recent ones for first time. After that all the messages will be synced.

## General Mappings
### Threads
Every thread will be represented by a room in matrix.  
For example a thread with id `xyz` will be represented by room alias `#_gmail_bridge_xyz.emailofuser_at_example.com` 
> Emails are added to alias to avoid conflicts when multiple users of bridge are part of same thread.

### Email Addresses
Every Email Address is mapped to a matrix (virtual) user.  
For example an email address `alice@example.com` will be represented by `@_gmail_bridge_alice_at_example.com`


## Encryption
There is no encryption as of now. And even when it'll be implemented, it won't be end-to-end
as we will need to decrypt the message before sending it. 
