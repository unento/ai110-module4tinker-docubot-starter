# DocuBot Model Card

This model card is a short reflection on my DocuBot system after implementing retrieval and experimenting with all three modes:

1. Naive LLM over full docs  
2. Retrieval only  
3. RAG (retrieval plus LLM)

All examples below come from running the same 11 questions, worded identically, through all three modes (the 8 questions in `dataset.py` plus 3 extra probes).

---

## 1. System Overview

**What is DocuBot trying to do?**  

> DocuBot answers developer questions about a codebase using the project's own documentation (`docs/`). The goal is to give answers that are grounded in what the docs actually say, cite where the answer came from, and refuse instead of guessing when the docs don't cover a question.

**What inputs does DocuBot take?**  

> - A natural language question from the user (typed at the CLI, or the built-in sample questions)
> - The `.md` / `.txt` files in the `docs/` folder (API_REFERENCE.md, AUTH.md, DATABASE.md, SETUP.md)
> - The `GEMINI_API_KEY` environment variable (from `.env`), needed for naive and RAG modes. Retrieval-only mode works without it.

**What outputs does DocuBot produce?**

> - **Naive mode:** a free-form Gemini answer
> - **Retrieval-only mode:** up to 3 raw doc sections, each labeled with its filename
> - **RAG mode:** a short Gemini answer based only on the retrieved sections, citing the files it used
> - **Any mode with retrieval:** the exact message `I do not know based on these docs.` when there isn't enough evidence

---

## 2. Retrieval Design

**How does your retrieval system work?**  

> - **Chunking:** Each document is split at every markdown heading (`#`, `##`, `###`), giving 36 sections of about 25–70 words. A `###` subsection carries its parent `##` heading (e.g. "Tables › users") so words in the parent heading stay searchable. Tables and code blocks are never split.
> - **Tokenizing:** One shared `tokenize()` function is used for both docs and questions. It lowercases text, keeps runs of letters, digits and underscores (so `DATABASE_URL` survives), also indexes the parts of underscore names (`AUTH_SECRET_KEY` → `auth`, `secret`, `key`), removes stopwords ("the", "is", "how", …), and strips simple suffixes (`users` → `user`, `generated`/`generation` → `generat`).
> - **Index:** An inverted index maps each word to the set of section IDs that contain it.
> - **Scoring:** Each distinct question word found in a section counts once, weighted by IDF (rare words count more; a word found in every section counts 0). Matches in the section's heading count double.
> - **Choosing top snippets:** A section must match at least 2 distinct question words (or every word, for a one-word question). Survivors are sorted by score, sections scoring under 50% of the top score are dropped, and up to 3 are returned.

**What tradeoffs did you make?**  

> - **Simplicity over semantic understanding:** Keyword matching is fast, transparent and easy to debug, but it can't match synonyms ("fields" vs. "Column") or tell that "payment" changes the meaning of "refresh a payment token."
> - **Heading sections over paragraphs:** Splitting on blank lines produced 163 fragments, 96 of them under 8 words, and separated code examples from their labels. Heading sections are bigger but self-contained.
> - **Precision over recall in the guardrail:** Requiring 2 matched words blocks stray matches (the payment question only matched "docs") but can refuse valid questions with typos ("databse").
> - **Not refusing on unknown words:** Refusing whenever a question contains a word found in no doc would catch "payment" and "MongoDB", but it would also refuse the valid sample question "Which fields are stored in the users table?" I left those cases to the LLM in RAG mode.
> - **Parameters tuned on a small set:** `MIN_MATCHED_WORDS = 2`, `HEADING_BOOST = 2` and `RELATIVE_SCORE_CUTOFF = 0.5` were chosen by testing on these 11 questions and may not carry over to other docs.

---

## 3. Use of the LLM (Gemini)

The model is `gemini-3.5-flash-lite`. The starter's `gemini-flash-lite-latest` returned a 404 from the current Google library, which calls the `v1` API, so I updated the name in `llm_client.py`.

**When does DocuBot call the LLM and when does it not?**  

> - **Naive LLM mode:** Always calls Gemini. Despite the menu label "over full docs," the starter prompt **does not include the docs**: `naive_answer_over_full_docs` ignores `all_text` and sends only the question. Every naive answer is therefore pure general knowledge.
> - **Retrieval-only mode:** Never calls Gemini. It returns the retrieved sections as-is.
> - **RAG mode:** Runs retrieval first. If no section has enough evidence, it refuses **without calling Gemini**. Otherwise it sends Gemini only the retrieved sections (up to 3) plus the question.

**What instructions do you give the LLM to keep it grounded?**  

> - Answer using only the information in the snippets.
> - Don't invent functions, endpoints or configuration values.
> - If the snippets aren't enough, or the question asks about something they don't cover, reply exactly `I do not know based on these docs.`
> - When answering, mention which files were used.
>
> Gemini's reply is also checked in code: if it starts with the refusal (even wrapped in quotes or followed by extra text), DocuBot returns the exact standard message, so all three refusal points produce identical output.

---

## 4. Experiments and Comparisons

| Query | Naive LLM: helpful or harmful? | Retrieval only: helpful or harmful? | RAG: helpful or harmful? | Notes |
|------|---------------------------------|--------------------------------------|---------------------------|-------|
| Where is the auth token generated? | Unhelpful: asked me to provide the codebase | Helpful: *Token Generation* first, plus *Environment Variables* | Helpful: "`generate_access_token` in `auth_utils.py`", cites AUTH.md | Naive admits it has no context, because the docs aren't in its prompt |
| What environment variables are required for authentication? | **Harmful:** lists `JWT_SECRET`, `GOOGLE_CLIENT_ID`, `SUPABASE_URL`, etc., none of which exist here | Helpful but noisy: right sections, plus a filler intro paragraph | **Partly wrong:** says `TOKEN_LIFETIME_SECONDS` must be configured, but SETUP.md lists it as optional | RAG failure, see §5 |
| How do I connect to the database? | **Harmful:** invents `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` | Helpful: *Connection Configuration* has the full answer | Helpful: `DATABASE_URL`, SQLite default, PostgreSQL format, `db.py` | Best example of RAG combining 2 sections cleanly |
| Which endpoint lists all users? | **Harmful:** `GET /users` (the real path is `/api/users`); misses admin-only | Accurate but hard to read: 3 endpoints, ~90 lines including JSON | Helpful: "`GET /api/users`", cites API_REFERENCE.md | RAG omitted the admin-only restriction |
| What does the /api/projects/<project_id> route return? | **Harmful:** invents `created_at`, `updated_at`, sample data "Website Redesign" | Helpful: correct endpoint first, but also returns the list endpoint and DB table | Helpful: quotes the documented response | |
| Is there any mention of payment processing in these docs? | Unhelpful: asked me to provide the docs | **Correct refusal** | **Correct refusal** (no API call made) | Only "docs" matched, so the guardrail refused at retrieval |
| How does a client refresh an access token? | **Harmful:** long OAuth 2.0 tutorial (`grant_type=refresh_token`, `client_secret`) unrelated to this API | Helpful: *Client Workflow* plus *POST /api/refresh* | Mostly helpful, but repeats a contradiction in the docs without flagging it | RAG failure, see §5 |
| Which fields are stored in the users table? | **Harmful:** "Based on the database schema…" then invents `username`, `created_at`, `updated_at` | Accurate but noisy: users table first, plus projects and permissions tables | Helpful: exactly `user_id`, `email`, `password_hash`, `joined_at` | Naive claims a source it never saw |
| How do I refresh a payment token? | **Harmful:** generic payment-gateway advice (`/tokens/refresh`, Stripe/PayPal) | **Misleading:** returns token refresh docs; "payment" ignored | **Correct refusal** | Only RAG noticed the docs say nothing about payments |
| How do I connect to the MongoDB database? | **Harmful:** full Node.js MongoDB driver tutorial | **Misleading:** returns SQLite/PostgreSQL connection docs | Safe refusal, but unhelpful | A better answer: "the docs cover SQLite and PostgreSQL, not MongoDB" |
| How do I deploy to Kubernetes? | Generic but reasonable kubectl/Helm steps, not from these docs | **Correct refusal** | **Correct refusal** | |

**What patterns did you notice?**  

> - **When naive LLM looks impressive but untrustworthy:** Almost always. The longest, most polished answers (the OAuth refresh tutorial, the MongoDB code sample, the users-table schema) were the least grounded. The worst case is "Which fields are stored in the users table?": it opens with *"Based on the database schema…"*, sounding like it read the docs, then lists `username`, `created_at` and `updated_at`, which don't exist. The real table has `user_id`, `email`, `password_hash` and `joined_at`. Naive mode never refused. It either guessed confidently or asked me to paste the docs.
> - **When retrieval only is clearly better:** Whenever accuracy matters more than readability. Every sentence it returns is verbatim from the docs, so it can't make anything up. The cost is that you have to read it yourself: for "Which endpoint lists all users?" the answer (`GET /api/users`, admin-only) is buried in ~90 lines covering three endpoints and their JSON. It's also blind to meaning: "refresh a **payment** token" and "connect to the **MongoDB** database" both returned confident-looking but irrelevant sections.
> - **When RAG is clearly better than both:** On direct factual questions it gave short, correct, cited answers using only facts present in the retrieved sections ("`GET /api/users`", the exact users-table columns, the `DATABASE_URL` connection rules). It was also the only mode that correctly refused "How do I refresh a payment token?", because the LLM could see the retrieved sections said nothing about payments.

---

## 5. Failure Cases and Guardrails

**Describe at least two concrete failure cases you observed.**  

> **Failure case 1: RAG made an unsupported claim by merging sources.**  
> *Question:* "What environment variables are required for authentication?"  
> *What happened:* RAG listed `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS` and said "both variables must be configured before starting the server," citing SETUP.md and AUTH.md.  
> *Why it's wrong:* AUTH.md does say that, but it also says `TOKEN_LIFETIME_SECONDS` "defaults to 3600 seconds if not set," and SETUP.md lists it under **Optional Variables**. That section wasn't retrieved. The section that was retrieved, *Required Variables*, lists `AUTH_SECRET_KEY` and `DATABASE_URL`.  
> *What should have happened:* `AUTH_SECRET_KEY` is required; `TOKEN_LIFETIME_SECONDS` is optional (default 3600). Ideally it would also note that the docs contradict each other.

> **Failure case 2: RAG repeated a contradiction in the docs without noticing it.**  
> *Question:* "How does a client refresh an access token?"  
> *What happened:* RAG said a client refreshes "when it expires by calling `/api/refresh`" and that the endpoint "exchanges an existing valid token."  
> *Why it's a problem:* Those two statements conflict. AUTH.md's *Validating Requests* section says expired tokens get a 401, so a client that waits until expiry may be unable to refresh. RAG stated both without flagging the conflict.  
> *What should have happened:* Say that `/api/refresh` needs a still-valid token, so clients should refresh **before** expiry, and point out that AUTH.md's wording ("when it expires") is ambiguous.

> **Failure case 3: Retrieval-only mode returns relevant-looking but wrong context.**  
> *Question:* "How do I refresh a payment token?" (and similarly "How do I connect to the MongoDB database?")  
> *What happened:* Retrieval-only mode returned the token refresh sections (or the SQLite/PostgreSQL connection docs) as if they answered the question.  
> *What should have happened:* Refuse, or make clear these docs don't cover payments or MongoDB. Keyword matching ignored the one word that mattered, because it appears in no doc.

**When should DocuBot say "I do not know based on these docs"?**  

> - When the topic isn't in the docs at all: "Is there any mention of payment processing in these docs?", "How do I deploy to Kubernetes?"
> - When the question has nothing meaningful to search for: an empty question, or only stopwords like "What is it?"
> - When retrieved sections share keywords with the question but don't answer it: "How do I refresh a **payment** token?" matches refresh docs that say nothing about payments.
> - When answering would require inventing specifics the docs don't contain, such as configuration for a database or framework the project doesn't use (MongoDB).

**What guardrails did you implement?**  

> 1. **Minimum evidence per section** (`has_meaningful_evidence`): a section must match at least 2 distinct meaningful question words (or every word, for a one-word question). Stopwords don't count.
> 2. **Single refusal decision** (`lacks_evidence`): if no section passes, both retrieval-only and RAG mode return the refusal. RAG refuses **before** calling Gemini, so the model never gets an empty context.
> 3. **Relative score cutoff:** sections scoring under 50% of the best match are dropped, so filler like a file's intro paragraph doesn't pad the results.
> 4. **Snippet limit:** at most 3 sections are passed to the LLM.
> 5. **Grounding prompt:** Gemini is told to use only the snippets, not invent values, cite files, and give the exact refusal when the snippets don't cover the question.
> 6. **One refusal message:** `REFUSAL_MESSAGE` is shared by all three refusal points, and Gemini's replies are normalized to it. Previously the code used two different wordings.

---

## 6. Limitations and Future Improvements

**Current limitations**  

1. **No understanding of meaning in retrieval.** Synonyms don't match ("fields" vs. "Column"), and a single unknown word that changes the meaning ("payment", "MongoDB") is ignored, so retrieval-only mode returns misleading context.
2. **Retrieval can miss context the answer needs.** The RAG answer about environment variables was wrong because SETUP.md's *Optional Variables* section wasn't retrieved, even though *Required Variables* was.
3. **No conflict detection.** When docs contradict each other (refresh "when it expires" vs. needing a "valid token"; `TOKEN_LIFETIME_SECONDS` "must be configured" vs. "defaults to 3600"), RAG repeats or merges the statements without warning.
4. **Typos cause wrong refusals.** "How do I connect to the databse?" is refused even though the docs answer it.
5. **Naive mode doesn't actually use the docs.** The starter prompt ignores `all_text`, so the "over full docs" label is misleading.
6. **API limits and errors.** The free tier allows 15 requests per minute. Running all questions through two LLM modes hit a `429 RESOURCE_EXHAUSTED` error, which was shown as if it were the answer.

**Future improvements**  

1. **Embedding-based (semantic) retrieval**, or a hybrid with keywords, so synonyms match and meaning-changing words like "payment" affect retrieval.
2. **Better context selection:** when a `###` section is retrieved, also include its sibling sections (e.g. *Optional Variables* next to *Required Variables*), so the LLM sees the complete picture.
3. **Prompt rules for conflicts and partial answers:** tell the model to flag contradictions between snippets, and when a question asks about something unsupported (MongoDB), say what the docs *do* cover rather than only refusing.
4. **Handle rate limits gracefully:** retry after the delay the API suggests, and show a clear "service unavailable" message instead of the raw error.

---

## 7. Responsible Use

**Where could this system cause real world harm if used carelessly?**  

> - **Naive mode invents configuration that looks real.** A developer who sets `DB_HOST`/`DB_USER` or `JWT_SECRET` because naive mode said so would waste time, or worse, deploy with security settings that don't apply (the real signing key is `AUTH_SECRET_KEY`).
> - **RAG answers sound authoritative even when slightly wrong.** The claim that `TOKEN_LIFETIME_SECONDS` must be set, and the unflagged refresh-timing contradiction, could lead to misconfigured auth or clients that fail to refresh tokens in production.
> - **Retrieval-only output can look like an answer when it isn't.** Returning the SQLite/PostgreSQL docs for a MongoDB question could lead someone to assume support that doesn't exist.
> - **Auth and security questions carry the most risk.** Wrong answers about token signing, expiry or permissions can create real vulnerabilities.

**What instructions would you give real developers who want to use DocuBot safely?**  

- Use RAG mode, not naive mode, for project-specific questions, and check the cited file before acting on an answer.
- Treat answers about security, authentication and configuration as pointers to the docs, not final answers; verify them in the source files and code.
- Treat "I do not know based on these docs" as a useful signal that the docs may have a gap, not a failure to work around with naive mode.
- Keep the docs current and consistent. DocuBot can only be as accurate as the docs, and it won't warn you when they contradict each other.
