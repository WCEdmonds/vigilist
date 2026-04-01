# AI-Assisted Document Review — Supplemental Specification

## Companion to: EDISCOVERY_PLATFORM_REQUIREMENTS.md (Phase 3)

**Date:** March 24, 2026
**Context:** This document expands Phase 3 (AI Features) of the main requirements spec with a structured AI-assisted review workflow modeled on industry best practices from TAR, active learning, and generative AI document review. It should be built after Phases 1 and 2 are functional.

---

## 1. AI Document Titling

Since this production has bare-minimum metadata (no subject lines, no author fields, no dates), documents are identified only by Bates number, which gives reviewers zero context when scanning a document list. The system should generate a short, human-readable title for every document using the Claude API.

### 1.1 Title Generation

- For each document, send the first ~1,000 tokens of extracted text to the Claude API with a prompt requesting a concise descriptive title (max ~80 characters).
- The title should capture: document type, key subject matter, and any identifiable parties or dates.
- Examples of good generated titles:
  - `Email re: Use of Force Complaint - Det. Johnson - 11/14/2024`
  - `Internal Affairs Investigation Report - Incident #2024-0382`
  - `Body Camera Video Transcript - Traffic Stop - 9/3/2024`
  - `Training Manual - Use of Force Policy (Excerpt)`
  - `Text Messages - Officer Davis to Sgt. Miller - Oct 2024`
  - `Voicemail Recording Transcript - Complainant Follow-Up`
- For native video/audio files with minimal or no extracted text, the title should note the file type and any available context (e.g., `MP4 Video - SCHLEGEL 000007 (No Extracted Text)`).

### 1.2 When to Run

- Runs automatically as part of **Ingest Phase B** (see main requirements doc, Section 3.3.2). Each document's title is generated as a task in the background job queue after Phase A makes documents browsable.
- Uses **claude-haiku-4-5-20251001** for cost efficiency — title generation is a lightweight task.
- Documents are batched (e.g., 20 per API call) and parallelized across workers for throughput.
- Documents appear in lists with Bates numbers immediately after Phase A; AI titles populate progressively as Phase B workers complete them.
- Titles can also be regenerated on demand (e.g., after prompt refinement or if the attorney wants a different style).

### 1.3 Data Model

- Add `ai_title` (string, nullable) to the document record.
- Add `ai_doc_type` (string, nullable) — a short classification tag generated alongside the title (e.g., "email", "report", "video", "memo", "text messages", "voicemail", "spreadsheet").
- Both fields are displayed in document lists, search results, and the viewer header.
- Titles are editable by the attorney (override with a manual title if the AI gets it wrong).

### 1.4 Display

- In all document lists and search results, show: `[ai_doc_type badge] ai_title — SCHLEGEL 000XXX`
- If no AI title has been generated yet, fall back to Bates number only.
- The document viewer header shows the AI title prominently, with the Bates range below it.

---

## 2. AI Review Workflow

The AI review feature follows a prompt-based generative AI workflow with human-in-the-loop validation. This is distinct from the ad hoc AI features (search, summarize, find similar) — this is a structured, end-to-end review pipeline.

### 2.1 Workflow Steps

**Step 1: Define Review Criteria (Prompt Engineering)**

- The attorney creates a **review prompt** describing the matter and what constitutes a responsive document. This is free-text, written in natural language.
- Example: *"This case involves allegations of excessive force by Anne Arundel County police officers against multiple plaintiffs. A document is responsive if it discusses use of force by officers, complaints about officer conduct, internal affairs investigations, disciplinary actions, training records related to use of force, or communications between officers about incidents involving the plaintiffs."*
- The system stores the prompt as a named **Review Project**. Multiple review projects can exist per case (e.g., one for responsiveness, one for privilege, one for issue coding).
- The prompt can be edited and versioned. Each version is stored so the team can track how criteria evolved.

**Step 2: Sample Analysis**

- The system selects a random or stratified sample of documents (default: 50 documents, configurable).
- Each sample document's extracted text is sent to the Claude API along with the review prompt.
- The API returns, for each document:
  - **Decision:** Responsive / Not Responsive / Needs Human Review
  - **Confidence score:** 0–100
  - **Reasoning:** 2–4 sentence explanation of why the document was classified this way
  - **Key excerpts:** Specific text passages from the document that drove the decision (with character offsets for highlighting)
  - **Considerations:** Any caveats or factors the reviewer should weigh (e.g., "This document discusses training generally but does not reference specific incidents involving the plaintiffs")

**Step 3: Attorney Review of Sample Results**

- The attorney reviews the AI's decisions on the sample set in a dedicated review interface.
- For each document, the attorney can:
  - **Agree** with the AI's decision (confirming it)
  - **Override** the AI's decision (correcting it, with an optional note explaining why)
  - **Flag** the document for discussion
- The system tracks agreement rate on the sample. If agreement is below a configurable threshold (default: 80%), the system prompts the attorney to refine the review criteria before proceeding.
- The attorney can edit the prompt and re-run the sample analysis.

**Step 4: Full Corpus Analysis**

- Once the attorney is satisfied with sample results, they trigger full corpus analysis.
- The system processes all documents against the review prompt via the Claude API.
- Processing is batched and parallelized for throughput (target: 500+ documents/hour, depending on document length and API rate limits).
- Each document receives the same structured output as in the sample phase (decision, confidence, reasoning, excerpts, considerations).
- Progress is displayed in real time (documents processed / total, estimated time remaining).
- The process can be paused and resumed.

**Step 5: Validation**

- After full corpus analysis, the system computes validation metrics (see Section 4).
- The attorney reviews a validation sample (randomly selected from AI-coded documents) to confirm accuracy.
- The system generates a validation report suitable for inclusion in a certification or motion regarding the review methodology.

### 2.2 Review Project Data Model

Each **Review Project** stores:

- `project_id` (primary key)
- `project_name` (e.g., "Responsiveness Review", "Privilege Review")
- `prompt_text` (current review criteria)
- `prompt_versions` (array of timestamped previous versions)
- `sample_size` (configurable, default 50)
- `agreement_threshold` (configurable, default 0.80)
- `status` (draft, sampling, reviewing_sample, running, complete, paused)
- `created_at`, `updated_at`

Each **AI Review Result** stores:

- `result_id` (primary key)
- `project_id` (foreign key)
- `doc_id` (foreign key)
- `ai_decision` (responsive, not_responsive, needs_review)
- `confidence_score` (0–100)
- `reasoning` (text)
- `key_excerpts` (JSON array of {text, start_offset, end_offset})
- `considerations` (text)
- `attorney_decision` (null until reviewed; agree, override_responsive, override_not_responsive)
- `attorney_note` (optional free text)
- `prompt_version` (which version of the prompt was used)
- `api_model` (which Claude model was used)
- `api_cost_tokens` (input + output token count for cost tracking)
- `created_at`

---

## 3. AI Review Interface

### 3.1 Review Queue

- Documents are presented in a queue, sorted by confidence score (ascending — least confident first, so the attorney reviews the hardest calls).
- Alternatively, sort by: most confident responsive first (for quick wins), most confident not responsive first (for exclusion review), or random.
- The queue shows: Bates range, AI decision badge, confidence score, first line of reasoning, and the production set.

### 3.2 Document Review Panel

The review panel displays, side by side:

**Left panel — AI Analysis:**
- AI decision (color-coded badge: green=responsive, red=not responsive, yellow=needs review)
- Confidence score (visual meter)
- Reasoning (full text)
- Key excerpts (clickable — clicking scrolls to and highlights the passage in the document viewer)
- Considerations
- Attorney action buttons: Agree / Override / Flag

**Right panel — Document Viewer:**
- Standard image viewer (TIFF pages) with text overlay panel
- Key excerpts from the AI analysis are highlighted in the text panel (yellow highlight)
- Search-within-document is still available
- Metadata panel (collapsible)

### 3.3 Prompt Refinement Interface

- Side panel showing the current review prompt with an inline editor.
- Below the prompt: a live summary of sample results (agreement rate, distribution of decisions, common override reasons).
- "Re-run Sample" button that re-processes the sample with updated prompt.
- Prompt version history with diff view.

---

## 4. Validation Metrics & Reporting

The system must compute and display the following metrics, consistent with industry-standard e-discovery validation practices.

### 4.1 Metrics

| Metric | Definition | Computation |
|--------|-----------|-------------|
| **Richness** | Percentage of responsive documents in the population | responsive_count / total_count |
| **Recall** | Percentage of truly responsive documents found by AI | true_positives / (true_positives + false_negatives) |
| **Precision** | Percentage of AI-responsive documents that are truly responsive | true_positives / (true_positives + false_positives) |
| **Elusion Rate** | Percentage of AI-not-responsive documents that are actually responsive | false_negatives / (false_negatives + true_negatives) |
| **F1 Score** | Harmonic mean of precision and recall | 2 × (precision × recall) / (precision + recall) |
| **Confidence Level** | Statistical confidence that metrics are accurate given sample size | Computed via binomial confidence interval on validation sample |

### 4.2 Validation Workflow

1. After full corpus analysis, the system selects a **validation sample** from the AI-coded documents (default: statistically significant sample at 95% confidence level ± 2% margin of error — the system should compute the required sample size based on corpus size).
2. The attorney reviews the validation sample and records agree/override for each.
3. The system computes all metrics from the validation sample.
4. Results are displayed in a dashboard with visual gauges for each metric.
5. If metrics fall below configurable thresholds (e.g., recall < 80%, precision < 70%), the system flags the review for prompt refinement.

### 4.3 Validation Report Export

Generate a PDF or Word document containing:

- Review project name and description
- Review prompt (final version)
- Corpus size and production sets included
- Sample sizes (initial sample + validation sample)
- All computed metrics with confidence intervals
- Distribution charts (responsive vs. not responsive, confidence score histogram)
- Attorney override rate and common override reasons
- AI model used and date of analysis
- Methodology description (suitable for inclusion in a certification to the court)

---

## 5. Issue Coding via AI

Beyond binary responsive/not responsive, the system should support **multi-issue coding** using the same prompt-based workflow.

### 5.1 Setup

- The attorney defines multiple **issues** for a review project, each with its own description.
- Example issues for this case:
  - "Use of excessive force"
  - "Failure to intervene"
  - "Inadequate training or supervision"
  - "Pattern or practice of misconduct"
  - "Spoliation or destruction of evidence"
  - "Redaction disputes"
- Each document can be tagged with zero or more issues.

### 5.2 AI Analysis

- The Claude API call includes all issue definitions along with the document text.
- The API returns, for each issue: applicable (yes/no), confidence, reasoning, and supporting excerpts.
- The attorney can agree or override each issue tag independently.

### 5.3 Issue Dashboard

- Matrix view: documents × issues, with color-coded cells showing AI confidence.
- Filter by issue to see all documents tagged for a specific issue.
- Export issue-coded document sets.

---

## 6. Privilege Review

Privilege review requires special handling due to its sensitivity and the risk of inadvertent disclosure.

### 6.1 Privilege Prompt Template

The system should include a default privilege review prompt template covering:

- Attorney-client privilege (communication between attorney and client for purpose of legal advice)
- Work product doctrine (materials prepared in anticipation of litigation)
- Common interest privilege (communications between parties with shared legal interest)

The attorney customizes the template with case-specific details (who are the attorneys, who are the clients, relevant firms, etc.).

### 6.2 Conservative Defaults

- For privilege review, the AI default threshold should be conservative: any document with a confidence score above a low threshold (e.g., 30%) for potential privilege should be flagged for human review rather than auto-coded.
- The system should never auto-exclude a document from privilege review without human confirmation.

### 6.3 Privilege Log Generation

- For documents coded as privileged, the system generates a draft privilege log entry using the AI reasoning and metadata.
- Fields: Bates range, date, from, to, cc, document type, privilege basis, description.
- The attorney edits and approves each entry before export.
- Export as CSV or formatted privilege log (Word/PDF).

---

## 7. Cost Management

AI review involves significant API usage. The system must provide cost visibility and controls.

### 7.1 Cost Tracking

- Track input and output tokens per API call.
- Display running cost for each review project (based on Claude API pricing).
- Show cost per document (average and distribution).
- Project estimated total cost before launching full corpus analysis.

### 7.2 Cost Controls

- Set a maximum budget per review project. The system pauses processing when the budget is reached and alerts the attorney.
- Option to use a smaller/cheaper model for initial pass and a more capable model for low-confidence documents only.
- Batch processing during off-peak hours if cost optimization is available.

---

## 8. Defensibility Considerations

AI-assisted review in litigation must be defensible. The system should support this by:

- **Audit trail:** Every AI decision, attorney override, prompt change, and validation action is logged with timestamps.
- **Reproducibility:** Store the exact prompt, model version, and API parameters used for each analysis so results can be explained and reproduced.
- **Transparency:** AI reasoning and cited excerpts are stored and exportable for each document, so the basis for every coding decision can be reviewed.
- **Methodology documentation:** The validation report (Section 4.3) provides a ready-made exhibit for court filings regarding the review methodology.
- **Human oversight:** The system is designed as AI-assisted, not AI-autonomous. Every decision is subject to attorney review, and the system never produces or withholds documents without human confirmation.

---

## 9. Integration with Main Platform

This AI review module integrates with the core platform as follows:

- **Tags:** AI decisions are stored as a special tag category ("AI Review: [Project Name]") that appears alongside manual tags in the main document viewer and search filters.
- **Search:** "AI Decision" and "AI Confidence" become available as advanced search filters (e.g., "Show all documents where AI confidence < 60 for Responsiveness Review").
- **Export:** AI-reviewed document sets can be exported using the same export pipeline as manually tagged sets (Phase 4), with the addition of AI reasoning columns in the load file.
- **Notes:** AI reasoning is linked to the document's notes/comments feed, clearly labeled as AI-generated.

---

## 10. Build Sequence (within Phase 3)

1. **API integration layer:** Claude API client with batching, retry, rate limiting, token counting, and cost tracking.
2. **Review Project CRUD:** Create, configure, and manage review projects with prompt versioning.
3. **Sample analysis pipeline:** Select sample, send to API, parse structured response, store results.
4. **AI review interface:** Review queue, document viewer with AI panel, agree/override workflow.
5. **Full corpus analysis pipeline:** Batched processing with progress tracking, pause/resume.
6. **Validation metrics engine:** Statistical sampling, metric computation, dashboard.
7. **Validation report export.**
8. **Issue coding extension.**
9. **Privilege review module with privilege log generation.**
10. **Cost management dashboard.**
