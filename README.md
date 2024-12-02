# organaiser-discord-bot
A Discord bot to be used in private servers to help keep you organised

# TODO:
- [ ] Add a 'timed reminder' function that the bot can call to be reminded of something at a particular time/date (optionally repeating)
- [ ] Automate the system prompt date-change rollover
- [ ] Automate the generation of a diary entry
- [ ] Allow AI to manage personal TODO lists via a function
- [ ] Allow AI to create 'memories' within its system prompt which have an expiry date
- [ ] Figure out the datetime comparison error when bot reconnects to Discord (maybe on_ready gets called multiple times?)
- [ ] Some kind of RAG framework (preferably using vector embeddings for semantic search)
- [ ] Ability for bot to read from and write to markdown notes contained in RAG framework