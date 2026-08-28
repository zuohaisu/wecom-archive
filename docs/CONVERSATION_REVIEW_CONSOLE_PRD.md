# Conversation Review Console PRD

> **Status:** historical feature PRD. It defines a review-console problem and
> interaction model, not the current product/deployment/identity architecture.
> Use [`architecture/current-state.md`](architecture/current-state.md) for
> current authority boundaries.

Related issue: RND-99

---

## 1. Problem Statement

Crowntime WeCom Archive currently stores archived messages and exposes basic message search, but the review experience is still too message-centric.

Reviewers do not primarily want to inspect isolated messages. They want to inspect conversations:

- What conversations exist for a monitored WeCom account?
- What contacts has a monitored account interacted with?
- What conversations exist for a contact across monitored accounts?
- What is the ordered message timeline inside a selected direct conversation or group conversation?

The review console should make `Conversation` the first-class review object. `Message` is the content inside a conversation, not the primary object the user navigates from.

The goal is a lightweight, functional, WeCom-inspired review console. Bootstrap-style or Ant Design-style UI is enough. The priority is information architecture and review workflow, not high-fidelity visual design.

---

## 2. Target Users

| User | Need |
|---|---|
| 365 administrator | Review archived WeCom conversations for operational, compliance, or support purposes. |
| Internal reviewer | Find conversation context around a monitored account, contact, group room, keyword, or time range. |
| Developer / QA reviewer | Validate that mock data, aggregation APIs, and UI behavior support the intended review workflow. |

This console is admin-only. It is not intended for monitored-account self-service, external contacts, or public access.

---

## 3. Product Model

The product model has two valid navigation paths.

```text
Monitored Account
        |
        v
  Conversation
        |
        v
     Messages
```

```text
Contact
        |
        v
  Conversation
        |
        v
     Messages
```

Both paths are required:

- Monitored-account path: review all conversations involving a monitored account.
- Contact path: review all conversations involving a contact, potentially across multiple monitored accounts.

Production may initially monitor only one account, but the model must support multiple monitored accounts later.

---

## 4. Core Review Modes

The console must support two primary review modes.

### Monitored Account-Centered Review

The reviewer starts from a monitored account and sees all archived conversations involving that account.

Primary question:

> "Show me all conversations involving this monitored account."

### Contact-Centered Review

The reviewer starts from a contact and sees all archived interactions involving that contact, potentially across multiple monitored accounts.

Primary question:

> "Show me every archived interaction we have with this contact."

Contact-centered review is not a lower-priority variant. It is required because the same contact may interact with multiple monitored accounts over time.

---

## 5. Key Concepts

| Concept | Definition |
|---|---|
| Monitored account | A WeCom account whose archived conversations are included in review scope. Examples may include a service account, sales account, executive account, or other approved internal account. The MVP may contain one monitored account, but the model must support multiple monitored accounts. |
| Contact | A counterpart identity encountered in archived conversations. A contact may be an individual, external organization contact, bot/system identity, or other non-monitored participant identity. Use `Contact`, not `Customer`, because not every WeCom counterpart is necessarily a customer. |
| Conversation | The first-class review object. A conversation groups messages into either a direct conversation or a group conversation. The console navigates to conversations first, then displays messages inside the selected conversation. |
| Direct conversation | A one-to-one conversation between a monitored account and a contact, represented by messages where `roomid` is empty or null. |
| Group conversation | A WeCom group conversation with a stable `roomid`. A group conversation may include monitored accounts, contacts, and other participants. |
| Conversation type | The classification of a conversation: `direct` or `group`. This type should be explicit because the UI and aggregation behavior will differ. |
| Message | A single archived WeCom message inside a conversation. Messages are timeline items, not the primary navigation object. |
| Message timeline | The ordered sequence of messages for a selected direct conversation or group conversation, sorted by message time. |

---

## 6. Conversation Object

The conversation object should exist conceptually even if the current database stores only raw messages. RND-98 should expose conversation-oriented API objects so RND-97 does not need to infer conversations in the UI.

### Minimum Conversation Fields

| Field | Purpose |
|---|---|
| `conversation_id` | Stable ID for the UI and API. |
| `conversation_type` | `direct` or `group`. |
| `display_name` | Name shown in the conversation list. For direct conversations, usually the contact label. For group conversations, the room label or `roomid` fallback. |
| `avatar_label` | Lightweight visual identity fallback such as initials or account/contact label. Real avatar support is later scope. |
| `monitored_account_ids` | Monitored account or accounts involved in the conversation. |
| `contact_ids` | Contacts involved in the conversation when known. |
| `roomid` | Present for group conversations. Empty for direct conversations. |
| `last_message_text` | Latest message snippet for scanning. |
| `last_message_time` | Latest message timestamp for sorting. |
| `message_count` | Number of messages included in the conversation scope. |
| `latest_sender_id` | Sender of the latest message. |
| `review_status` | Human or system-readable state such as `active`, `waiting_contact_reply`, `waiting_internal_reply`, or `closed`. MVP may return null or a simple placeholder. |
| `ai_status` | Future AI processing state such as `not_started`, `pending`, `ready`, or `failed`. MVP may return null. |
| `ai_summary` | Future conversation summary. MVP does not need to generate AI content, but the field should be planned as part of the model. |

### Conversation Summary Direction

MVP does not implement AI summarization, but the product shape should anticipate it.

Long-term conversation cards should evolve toward:

```text
Contact / Room Name
Last message: ...
Last active: yesterday
Recent activity: 12 messages in the last 7 days
Status: waiting for contact reply
AI Summary: contact asked about pricing; internal quote is pending
```

This means the UI should be designed around conversation-level scanning, not raw message lists.

---

## 7. Three-Column UI

The review console should use a simple three-column layout inspired by WeCom desktop patterns.

| Column | Purpose | Required behavior |
|---|---|---|
| Left | Mode and entity selector | Switch between monitored-account-centered and contact-centered modes. Search and select a monitored account or contact. |
| Middle | Conversation list | Show direct conversations and group conversations relevant to the selected entity. Support filters and conversation-level metadata. |
| Right | Message timeline | Show ordered messages for the selected conversation. Include sender, timestamp, content, message type, and direct/group context. |

### Layout Notes

- The selected mode and entity should remain visible while browsing conversations.
- The middle column is always a conversation list, not a message list.
- Selecting a conversation in the middle column updates the right timeline without changing the selected entity.
- Empty states should be explicit: no selected entity, no conversations found, no messages found.
- The interface should favor density and scanning over decorative layout.

---

## 8. Conversation List

Each row/card in the middle column represents one conversation.

Each conversation item should display at least:

| Element | MVP Requirement |
|---|---|
| Avatar / identity marker | Required. A text fallback is acceptable. |
| Name | Required. Contact name/ID for direct conversations; room name/ID for group conversations. |
| Conversation type | Required. Show direct vs group. |
| Last message | Required. Show a short snippet. |
| Last message time | Required. Used for sorting and scanning. |
| Message count | Required. |
| Related monitored account | Required in contact-centered mode; useful in account-centered mode when multiple accounts are involved. |
| Unread count | Later scope. Reserve model/UI space if practical. |
| AI status | Later scope. Reserve model/UI space if practical. |
| AI summary | Later scope. Do not generate in MVP. |

Default sort should be latest activity first.

---

## 9. Monitored Account-Centered Behavior

When the reviewer selects monitored-account-centered mode:

1. The left column lists monitored accounts.
2. The reviewer selects one monitored account.
3. The middle column shows all conversations involving that monitored account.
4. The conversation list includes:
   - direct conversations with contacts
   - group conversations where the monitored account appears
   - latest message time
   - latest message snippet
   - message count
   - conversation type
   - participant or room label
5. Selecting a conversation opens its message timeline in the right column.

For direct conversations, the timeline should include messages where the selected monitored account is either the sender or recipient.

For group conversations, the timeline should include messages in the selected `roomid`, with enough participant context to identify why the room appears for the selected monitored account.

---

## 10. Contact-Centered Behavior

When the reviewer selects contact-centered mode:

1. The left column lists contacts.
2. The reviewer selects one contact.
3. The middle column shows all direct conversations and group conversations involving that contact.
4. The list must support interactions across multiple monitored accounts, not only the current production monitored account.
5. The conversation list includes:
   - related monitored account or accounts
   - direct vs group indicator
   - latest message time
   - latest message snippet
   - message count
6. Selecting a conversation opens its message timeline in the right column.

The reviewer should not need to search each monitored account separately to reconstruct a contact's interaction history.

---

## 11. Search and Filter Behavior

Search and filter should be lightweight for MVP, but must support the review workflow.

### Entity Search

| Target | Behavior |
|---|---|
| Monitored account | Search by WeCom user ID and display name when available. |
| Contact | Search by WeCom user ID and display name when available. |

### Conversation List Filters

| Filter | Behavior |
|---|---|
| Keyword | Match message content within conversations and return the matching conversations. |
| Conversation type | Filter by `direct` or `group`. |
| Message type | Filter by text, image, voice, video, file, mixed, or other stored message types. |
| Time range | Filter conversations by message time range. |
| Related monitored account | In contact-centered mode, filter by monitored account when multiple accounts are involved. |

### Message Timeline Filters

The MVP may apply filters at the conversation-list level first. Later versions may support filtering inside the selected timeline.

Search results must preserve enough context that reviewers can understand why a conversation matched.

---

## 12. Mock Dataset Requirements for RND-96

RND-96 should create a mock dataset that can validate the conversation review workflow without real WeCom data, secrets, or production exports.

Required mock coverage:

| Requirement | Purpose |
|---|---|
| At least two monitored accounts | Proves the model does not hardcode a single monitored account. |
| At least three contacts | Supports contact selector and contact-centered review. |
| At least two direct conversations | Validates account-contact timelines without `roomid`. |
| At least two group conversations | Validates room-based conversation list and timeline behavior. |
| One contact interacting with multiple monitored accounts | Validates contact-centered aggregation across monitored accounts. |
| Multiple messages per conversation | Validates ordered timeline rendering and latest-message summaries. |
| Mixed sender/recipient direction | Validates inbound and outbound message display. |
| Multiple message types where feasible | Validates message type labels and filters. Text-only is acceptable for the first slice if non-text rows are represented as metadata-safe placeholders. |
| Time variation across messages | Validates sorting and time-range filtering. |
| Conversation-level metadata placeholders | Allows RND-97 to show message count, last message, last time, review status, AI status, and AI summary placeholder without generating real AI output. |

Mock data must remain fake and safe. It must not contain real contact records, PHI, credentials, API keys, production exports, or copied real conversations.

---

## 13. Aggregation API Requirements for RND-98

RND-98 should provide aggregation APIs that allow RND-97 to build the UI without hiding aggregation logic inside the front end.

Required API capabilities:

| Capability | Requirement |
|---|---|
| List monitored accounts | Return monitored accounts available for review, including stable ID and display label. |
| List contacts | Return contacts available for contact-centered review, including stable ID and display label. |
| List conversations by monitored account | Given a monitored account ID, return relevant direct conversations and group conversations. |
| List conversations by contact | Given a contact ID, return relevant direct conversations and group conversations across monitored accounts. |
| Get message timeline | Given a conversation key, return ordered messages with sender, recipients, timestamp, message type, content text, and direct/group metadata. |
| Search and filters | Support keyword, conversation type, message type, time range, and related monitored account filters where practical for MVP. |

### Conversation Identity

The API should expose stable conversation identity instead of making the UI infer it from raw messages.

Suggested identity model:

| Conversation type | Suggested key |
|---|---|
| Direct conversation | Normalized pair of monitored account ID and contact ID. |
| Group conversation | `roomid`. |

The API may internally use `archive_messages`, `archive_message_recipients`, and `contacts`, but the UI should consume review-oriented objects such as monitored account, contact, conversation, and timeline item.

---

## 14. UI Implementation Direction for RND-97

RND-97 should implement a functional review console using the aggregation API from RND-98.

Implementation direction:

- Use a simple server-rendered or lightweight front-end approach consistent with the existing FastAPI admin surface.
- Use Bootstrap-style or Ant Design-style layout conventions.
- Prioritize three-column information architecture over visual polish.
- Use WeCom-inspired spacing and review patterns, but do not attempt a pixel-perfect WeCom clone.
- Keep aggregation logic out of the UI.
- Treat empty, loading, and error states as required UI states.
- Preserve admin-only assumptions from the existing architecture.
- Build around conversation cards/list items, not message rows.

The first UI slice should prove that a reviewer can select a mode, select an entity, select a conversation, and read the message timeline.

---

## 15. MVP Scope

MVP includes:

- Monitored-account-centered review mode.
- Contact-centered review mode.
- `Conversation` as the first-class UI and API object.
- Explicit conversation type: `direct` or `group`.
- Three-column review layout.
- Entity selector for monitored accounts and contacts.
- Conversation list for direct conversations and group conversations.
- Message timeline for the selected conversation.
- Conversation-level metadata: display name, type, last message, last message time, message count, related monitored account, and placeholder status fields.
- Keyword search.
- Basic filters for conversation type, message type, time range, and related monitored account where applicable.
- Mock dataset coverage sufficient to demonstrate both review modes.
- Aggregation API support for monitored account, contact, conversation list, and timeline objects.

MVP does not require high-fidelity styling, real-time updates, advanced analytics, full media preview, unread state, or AI-generated summaries.

---

## 16. Later Scope

Later scope may include:

- AI-generated conversation summaries.
- AI processing status on conversation cards.
- AI search across conversations.
- AI timeline reconstruction and event extraction.
- AI agent workflows for review assistance, escalation, or follow-up drafting.
- Media preview and download controls with signed URLs.
- Advanced full-text search and highlighting.
- Reviewer audit log.
- Saved searches.
- Conversation export, subject to security approval.
- Better contact identity resolution.
- Monitored account and contact grouping.
- Room participant history.
- Pagination and virtualized timelines for large conversations.
- Admin permission tiers.
- Search result snippets inside long timelines.

Later scope must be approved separately before implementation.

---

## 17. Product Roadmap Direction

The long-term product direction is:

```text
Archive
  -> Review
  -> AI Summary
  -> AI Search
  -> AI Timeline
  -> AI Agent
```

RND-99 should not implement this roadmap. It should make sure the first review console model does not block it.

The most important implication is that the system should aggregate messages into conversations now, so future AI features can attach to the conversation layer instead of rebuilding the product model later.

---

## 18. Non-Goals

The following are not goals for RND-99 or the first review console slice:

- No code implementation in RND-99.
- No API change in RND-99.
- No mock data change in RND-99.
- No secrets, credentials, PHI, production exports, or real contact conversation data.
- No write-back to WeCom.
- No message deletion or editing.
- No monitored-account-facing or contact-facing user experience.
- No public access.
- No real-time chat replay.
- No AI summarization implementation in MVP.
- No automated judgment over conversations.
- No pixel-perfect WeCom UI clone.
- No assumption that production will always monitor only one account.

---

## 19. Acceptance Criteria

RND-99 is complete when:

- `docs/CONVERSATION_REVIEW_CONSOLE_PRD.md` exists.
- The PRD defines the problem statement, target users, product model, core review modes, key concepts, conversation object, three-column UI, conversation list, monitored-account-centered behavior, contact-centered behavior, search/filter behavior, RND-96 mock dataset requirements, RND-98 aggregation API requirements, RND-97 UI direction, MVP scope, later scope, roadmap direction, non-goals, and acceptance criteria.
- The PRD explicitly makes `Conversation` the first-class review object and treats `Message` as timeline content inside a conversation.
- The PRD explicitly uses `Monitored Account` and `Contact` as the primary domain language.
- The PRD explicitly distinguishes `direct` and `group` conversation types.
- The PRD explicitly defines conversation-level summary fields and future AI summary direction without requiring AI implementation in MVP.
- The PRD explicitly states that production may initially monitor one account but the model must support multiple monitored accounts later.
- The PRD explicitly supports both monitored-account-centered conversation review and contact-centered cross-account interaction review.
- The PRD keeps the UI direction functional and WeCom-inspired without requiring high-fidelity visual design.
- No application code, API behavior, mock dataset, secrets, or environment files are changed.
- `git status --short`, `git diff --stat`, and `git diff -- docs/CONVERSATION_REVIEW_CONSOLE_PRD.md` are run after editing.
