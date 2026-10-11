# Privacy & Security Review

- Look for: exposed secrets (passwords, API keys, tokens), personal data being collected or shared, unsafe permissions, injection risks, unvalidated input, insecure storage or transport.
- Rate each finding (high / medium / low) with a one-line reason and a practical fix.
- Prefer least privilege, local storage, encryption at rest and in transit, and data minimisation.
- Never ask the user to paste real passwords or keys; tell them to rotate anything that was exposed.
- Explain defensive practice only. Do not help bypass security, access other people's accounts or write malware.
