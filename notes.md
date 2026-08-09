ollama based text dungeon. must use minimal resources. 
probe ollama. complete client, focused on running lean. ollama will be used for room/dungeon generation and description, npc creation and making judgments, but optimized for the current machine which is very resource constrained. experiment with turning thinking on and off. use structured outputs. consider tool calling as part of suite of services.

use embeddings and graphs for maintaining a record of the world, recalling conversations, preserving continuity and themes. chromadb + neo4j in docker containers. i'm open to suggestion for storing other game related data, but small, portable, light such as sqlite, etc. a

start with the ollama client.

https://docs.ollama.com/api/introduction
https://docs.ollama.com/capabilities/thinking
https://docs.ollama.com/capabilities/structured-outputs
https://docs.ollama.com/capabilities/embeddings
https://docs.ollama.com/capabilities/tool-calling