"""
Core DocuBot class responsible for:
- Loading documents from the docs/ folder
- Building a simple retrieval index (Phase 1)
- Retrieving relevant snippets (Phase 1)
- Supporting retrieval only answers
- Supporting RAG answers when paired with Gemini (Phase 2)
"""

import os
import glob
import math
import re

# Common words that carry no meaning for retrieval. Without this list,
# words like "the" and "is" (which appear in every doc) dominate scoring.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "be", "been", "am",
    "i", "you", "we", "it", "this", "that", "these", "those", "there",
    "how", "what", "where", "which", "who", "when", "why",
    "do", "does", "did", "can", "should", "would", "will",
    "to", "of", "in", "on", "for", "with", "by", "at", "from", "as",
    "and", "or", "not", "any", "all", "my", "your", "its", "if", "so",
}


# A query word that appears in a section's heading counts this many times as much.
HEADING_BOOST = 2.0

# After sorting, keep only chunks scoring at least this fraction of the top chunk's score.
RELATIVE_SCORE_CUTOFF = 0.5

# -----------------------------------------------------------
# Guardrail: refusing when there is no useful context
# -----------------------------------------------------------
# "Useful context" means at least one section that matches MIN_MATCHED_WORDS
# distinct meaningful query words (or every query word, if the query has fewer
# words than that). Stopwords don't count, so "What is it?" has no evidence.
#
# The guardrail works in layers:
#   1. retrieve() drops any section that fails has_meaningful_evidence().
#   2. If no section survives, lacks_evidence() is True and both answer modes
#      return REFUSAL_MESSAGE without calling the LLM.
#   3. In RAG mode, the Gemini prompt tells the model to give the same
#      REFUSAL_MESSAGE when the sections don't actually answer the question.
#      This catches what keyword matching can't, e.g. "How do I refresh a
#      payment token?" matches refresh docs, but they say nothing about payments.
#
# Known limits of layer 1 (by design):
#   - Words found in no doc are ignored rather than forcing a refusal, so
#     "Which fields are stored in the users table?" still works even though the
#     docs say "Column". Layer 3 handles unknown topics like "payment" or "MongoDB".
#   - Typos reduce matches and may cause a refusal ("databse").
MIN_MATCHED_WORDS = 2
REFUSAL_MESSAGE = "I do not know based on these docs."


def normalize(word):
    """
    Very crude suffix stripping (a tiny "stemmer") so related word forms match:
      users -> user, generated / generation -> generat, connect / connection -> connect

    Only strips when at least 4 letters remain, so short words are left alone,
    and skips words ending in "ss" ("access", "class").
    """
    for suffix in ("ing", "ion", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            if suffix == "s" and word.endswith("ss"):
                continue
            return word[: -len(suffix)]
    return word


def tokenize(text):
    """
    Splits text into lowercase, normalized word tokens with stopwords removed.

    Keeps letters, digits, and underscores together so identifiers like
    DATABASE_URL survive, while punctuation, backticks, and markdown symbols
    are dropped ("database?" -> "database", "`AUTH_SECRET_KEY`" -> "auth_secret_key").

    Used for BOTH documents and queries so they are always processed the same way.
    """
    tokens = []
    for word in re.findall(r"[a-z0-9_]+", text.lower()):
        # Keep the full identifier AND its parts, so "AUTH_SECRET_KEY"
        # matches queries for "auth_secret_key", "auth", "secret", or "key".
        parts = [word] + (word.split("_") if "_" in word else [])
        for part in parts:
            if part and part not in STOPWORDS:
                tokens.append(normalize(part))
    return tokens


class DocuBot:
    def __init__(self, docs_folder="docs", llm_client=None):
        """
        docs_folder: directory containing project documentation files
        llm_client: optional Gemini client for LLM based answers
        """
        self.docs_folder = docs_folder
        self.llm_client = llm_client

        # Load documents into memory
        self.documents = self.load_documents()  # List of (filename, text)

        # Split documents into heading-based sections for retrieval
        self.chunks = self.chunk_documents(self.documents)  # List of (filename, section_text)

        # Build a retrieval index over the chunks (implemented in Phase 1)
        self.index = self.build_index(self.chunks)

    # -----------------------------------------------------------
    # Document Loading
    # -----------------------------------------------------------

    def load_documents(self):
        """
        Loads all .md and .txt files inside docs_folder.
        Returns a list of tuples: (filename, text)
        """
        docs = []
        pattern = os.path.join(self.docs_folder, "*.*")
        for path in glob.glob(pattern):
            if path.endswith(".md") or path.endswith(".txt"):
                with open(path, "r", encoding="utf8") as f:
                    text = f.read()
                filename = os.path.basename(path)
                docs.append((filename, text))
        return docs

    # -----------------------------------------------------------
    # Chunking
    # -----------------------------------------------------------

    def chunk_documents(self, documents):
        """
        Splits each document into sections at every markdown heading (#, ##, ###...).

        - Each ### subsection (e.g. a table or an API endpoint) becomes its own
          chunk, so tables and code blocks are never cut in half.
        - A ### chunk also gets its parent ## heading prepended, so words that only
          appear in the parent heading (e.g. "Tables") are still searchable.
        - Heading-only sections with no body (e.g. "## Tables") are skipped.
        - Lines inside ``` code fences are never treated as headings.
        - Files with no headings become a single chunk.

        Returns a list of (filename, section_text) tuples.
        """
        chunks = []
        heading_pattern = re.compile(r"^(#{1,6})\s+(.*)")

        for filename, text in documents:
            parent_heading = None  # most recent ## heading
            current_lines = []
            in_code_block = False

            def flush():
                body = [line for line in current_lines if not heading_pattern.match(line)]
                if "".join(body).strip():
                    chunks.append((filename, "\n".join(current_lines).strip()))

            for line in text.splitlines():
                if line.strip().startswith("```"):
                    in_code_block = not in_code_block

                match = None if in_code_block else heading_pattern.match(line)
                if match:
                    flush()
                    level = len(match.group(1))
                    if level <= 2:
                        parent_heading = line
                        current_lines = [line]
                    else:
                        current_lines = [parent_heading, line] if parent_heading else [line]
                else:
                    current_lines.append(line)

            flush()

        return chunks

    # -----------------------------------------------------------
    # Index Construction (Phase 1)
    # -----------------------------------------------------------

    def build_index(self, documents):
        """
        Builds an inverted index mapping each normalized word to the set of
        chunk ids (positions in `documents`) that contain it.

        Example structure:
        {
            "token": {11, 12, 15, 2, 3},
            "database": {17, 18, 19, 31}
        }

        Uses sets so a chunk is listed once per word no matter how often the
        word repeats. The index serves two purposes:
        - retrieve() uses it to find candidate chunks without scanning everything
        - idf() uses it to count how many chunks contain a word (rarity)
        """
        index = {}
        for chunk_id, (_, text) in enumerate(documents):
            for word in tokenize(text):
                index.setdefault(word, set()).add(chunk_id)
        return index

    # -----------------------------------------------------------
    # Scoring and Retrieval (Phase 1)
    # -----------------------------------------------------------

    def score_document(self, query, text):
        """
        Returns a relevance score for how well the text matches the query.

        - Each distinct query word found in the text counts once (repetition
          doesn't inflate the score, so long sections don't win by default).
        - Each match is weighted by IDF, so rare words matter more than common ones.
        - Matches in the section heading are multiplied by HEADING_BOOST.
        - Matching is on whole normalized tokens, never substrings.
        """
        text_words = set(tokenize(text))
        query_words = set(tokenize(query))

        # Words in a section's heading(s) describe what the section is about,
        # so a match there counts extra ("Token Generation", "Tables > users").
        heading_text = "\n".join(line for line in text.splitlines() if line.startswith("#"))
        heading_words = set(tokenize(heading_text))

        score = 0.0
        for word in query_words & text_words:  # each query word counted once
            weight = HEADING_BOOST if word in heading_words else 1.0
            score += self.idf(word) * weight
        return score

    def idf(self, word):
        """
        Inverse document frequency: rare words are worth more than common ones.
        A word found in every unit scores 0; a word found in only one scores highest.
        """
        units_with_word = len(self.index.get(word, ()))
        if units_with_word == 0:
            return 0.0
        return math.log(len(self.chunks) / units_with_word)

    def has_meaningful_evidence(self, query_words, text):
        """
        Guardrail layer 1: does this section contain enough of the question to count
        as evidence?

        A section qualifies if it matches at least MIN_MATCHED_WORDS distinct query
        words, or every query word when the query is shorter than that (so a
        one-word query like "token" can still be answered).

        A single stray match is not enough: for "Is there any mention of payment
        processing in these docs?" the only match is "docs", so nothing qualifies.
        """
        if not query_words:
            return False  # empty or stopword-only question: nothing to look for

        matched_words = query_words & set(tokenize(text))
        required_matches = min(MIN_MATCHED_WORDS, len(query_words))
        return len(matched_words) >= required_matches

    def lacks_evidence(self, snippets):
        """
        Guardrail layer 2: the single place where DocuBot decides to refuse.

        Returns True when retrieval found no section with meaningful evidence.
        Both answer modes check this before answering, and RAG mode checks it
        before calling the LLM, so the model never sees an empty context.
        """
        return len(snippets) == 0

    def retrieve(self, query, top_k=3):
        """
        Uses the index and scoring function to select the top_k most relevant
        sections (chunks) across all docs.

        Returns a list of (filename, section_text) sorted by score descending.
        Returns an empty list when nothing has enough evidence, which the
        answer modes turn into an "I do not know" refusal.
        """
        query_words = set(tokenize(query))

        # Use the index to find candidate chunks: only chunks that contain
        # at least one query word are worth scoring.
        candidate_ids = set()
        for word in query_words:
            candidate_ids |= self.index.get(word, set())

        scored = []
        for chunk_id in candidate_ids:
            filename, text = self.chunks[chunk_id]
            if not self.has_meaningful_evidence(query_words, text):
                continue  # guardrail layer 1: not enough evidence in this section
            score = self.score_document(query, text)
            if score > 0:
                scored.append((score, filename, chunk_id, text))

        # Highest score first; ties broken by filename then position for stable output
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))

        # Relative cutoff: drop sections that score far below the best match,
        # so weak filler (e.g. a file's intro paragraph) doesn't pad the results
        # just because top_k has room.
        if scored:
            min_score = scored[0][0] * RELATIVE_SCORE_CUTOFF
            scored = [item for item in scored if item[0] >= min_score]

        results = [(filename, text) for _, filename, _, text in scored]
        return results[:top_k]

    # -----------------------------------------------------------
    # Answering Modes
    # -----------------------------------------------------------

    def answer_retrieval_only(self, query, top_k=3):
        """
        Phase 1 retrieval only mode.
        Returns raw snippets and filenames with no LLM involved.
        """
        snippets = self.retrieve(query, top_k=top_k)

        if self.lacks_evidence(snippets):
            return REFUSAL_MESSAGE

        formatted = []
        for filename, text in snippets:
            formatted.append(f"[{filename}]\n{text}\n")

        return "\n---\n".join(formatted)

    def answer_rag(self, query, top_k=3):
        """
        Phase 2 RAG mode.
        Uses student retrieval to select snippets, then asks Gemini
        to generate an answer using only those snippets.
        """
        if self.llm_client is None:
            raise RuntimeError(
                "RAG mode requires an LLM client. Provide a GeminiClient instance."
            )

        snippets = self.retrieve(query, top_k=top_k)

        if self.lacks_evidence(snippets):
            return REFUSAL_MESSAGE  # refuse before calling the LLM

        return self.llm_client.answer_from_snippets(query, snippets)

    # -----------------------------------------------------------
    # Bonus Helper: concatenated docs for naive generation mode
    # -----------------------------------------------------------

    def full_corpus_text(self):
        """
        Returns all documents concatenated into a single string.
        This is used in Phase 0 for naive 'generation only' baselines.
        """
        return "\n\n".join(text for _, text in self.documents)
