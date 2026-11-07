# Company discovery and job-board ingestion

The previous free-feed-to-company design was superseded by the user's clarified
requirements. See [the clean data model](data-model.md).

Pituchim directory discovery may create pending Company records. Arbeitnow and
Himalayas supply **jobs only**, with reported employer text and their original source
and application links. They never populate the Company catalogue or attest employer
identity. Company career endpoints require explicit Company ownership.

Do not follow historical migration numbers, company-feed bootstrap instructions, or
board-to-company import examples from earlier development work. The development
schema is now one initial migration with one clean bootstrap command.
