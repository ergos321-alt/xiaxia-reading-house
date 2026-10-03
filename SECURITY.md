# Security

- Never commit `.env`, credentials, bearer tokens, or database URLs. Load secrets from server-side environment variables or secure runtime configuration.
- Keep the Supabase Storage bucket private. The Supabase service-role key must remain on the server and must never reach browser or client code.
- Uploaded books, reading progress, annotations, and related memories are private user data. Do not publish storage objects, database exports, logs, or real reading fixtures.
- Do not include credentials or private book content in public issues. Use GitHub private vulnerability reporting when enabled, or contact the maintainer privately through GitHub.
