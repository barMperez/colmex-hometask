# Spec Delta

## MODIFIED Requirements

### Requirement: Fail closed when policy cannot be evaluated
The system SHALL refuse to produce an eligibility assessment, and SHALL report a policy-not-evaluable failure, when no active policy configuration is available, when the official non-publication calendar has not been loaded for any year the evaluation needs, or when no required publication date exists within the configured maximum age for an observation that is neither future-dated nor over the maximum age.

#### Scenario: Calendar year not loaded
- **GIVEN** non-publication calendar data is loaded only for 2026 and 2027
- **WHEN** the evaluation instant is in 2028
- **THEN** the system reports policy-not-evaluable and does not return ELIGIBLE or NOT_ELIGIBLE

#### Scenario: No active policy
- **WHEN** no active policy version exists
- **THEN** the system reports policy-not-evaluable

#### Scenario: Required date falls in an unloaded year
- **GIVEN** calendar data is loaded for 2027 but not 2026
- **WHEN** the evaluation instant is 2027-01-04 09:00 Asia/Jerusalem and finding the required publication date requires checking days in 2026
- **THEN** the system reports policy-not-evaluable

#### Scenario: No business day within the maximum age
- **GIVEN** a maximum age of 7 days
- **AND** the calendar data wrongly lists every day from 2026-11-17 to 2026-11-26 as non-publication days
- **WHEN** the evaluation instant is 2026-11-26 18:00 Asia/Jerusalem and the latest observation is dated 2026-11-25
- **THEN** the system reports policy-not-evaluable and does not return ELIGIBLE or NOT_ELIGIBLE

#### Scenario: Over-age observation keeps its reason despite the calendar gap
- **GIVEN** the same maximum age and calendar data
- **WHEN** the evaluation date is 2026-11-26 and the latest observation is dated 2026-11-16
- **THEN** the assessment is NOT_ELIGIBLE with reason MAX_AGE_EXCEEDED
