# Spec Delta

## ADDED Requirements

### Requirement: Not-eligible pairs are observable
The readiness endpoint SHALL list every enabled pair that is not currently ELIGIBLE, with its reason and the publication date of its current observation. A pair with no observation SHALL be listed with reason RATE_NOT_FOUND. When the policy cannot be evaluated, every enabled pair SHALL be listed with reason POLICY_NOT_EVALUABLE. The list SHALL NOT affect the readiness status.

#### Scenario: Overdue pair listed
- **GIVEN** the required publication date is 2026-11-24 and the current USD observation is dated 2026-11-23, while EUR and GBP are dated 2026-11-24
- **WHEN** a client calls the readiness endpoint
- **THEN** the response status is 200
- **AND** `notEligiblePairs` contains exactly USD/ILS with reason PUBLICATION_OVERDUE and publication date 2026-11-23

#### Scenario: All pairs eligible
- **WHEN** every enabled pair's current observation is ELIGIBLE
- **THEN** `notEligiblePairs` is an empty list

#### Scenario: Policy cannot be evaluated
- **GIVEN** the evaluation instant falls in a year with no loaded calendar
- **WHEN** a client calls the readiness endpoint
- **THEN** the response status is 200 and every enabled pair is listed with reason POLICY_NOT_EVALUABLE

### Requirement: Not-eligible transitions are logged
After each retrieval attempt, the refresh process SHALL assess every enabled pair. It SHALL log at error level once when a pair becomes not eligible (or is not eligible at its first assessment after start-up), and at info level once when the pair becomes eligible again. It SHALL NOT repeat the error while the pair stays not eligible.

#### Scenario: Pair becomes overdue
- **GIVEN** USD/ILS was ELIGIBLE at the previous assessment
- **WHEN** the next assessment, after the 2026-11-24 deadline, finds the USD observation dated 2026-11-23
- **THEN** one error-level log line names USD/ILS, reason PUBLICATION_OVERDUE and the required publication date 2026-11-24

#### Scenario: Still overdue
- **WHEN** USD/ILS is still not eligible at the following assessments
- **THEN** no further error-level line is logged for USD/ILS

#### Scenario: Pair recovers
- **WHEN** a later assessment finds USD/ILS ELIGIBLE again
- **THEN** one info-level log line records the recovery

### Requirement: Calendar coverage expiry warning
The system SHALL compute the calendar coverage end as 31 December of the last year in the unbroken run of loaded calendar years starting at the current Asia/Jerusalem year. The readiness endpoint SHALL report that date, or null when the current year is not loaded, and a warning flag that is true when the end is null or no more than the configured number of days away. While the flag is true, the refresh process SHALL log a warning at most once per Asia/Jerusalem calendar day.

#### Scenario: Far from the end
- **GIVEN** calendars are loaded for 2026 and 2027 and the warning threshold is 60 days
- **WHEN** the current date is 2026-11-24
- **THEN** `calendarCoverageEndsOn` is 2027-12-31 and `calendarExpiryWarning` is false

#### Scenario: Within the threshold
- **GIVEN** the same calendars and threshold
- **WHEN** the current date is 2027-11-02
- **THEN** `calendarCoverageEndsOn` is 2027-12-31 and `calendarExpiryWarning` is true
- **AND** the refresh process logs one warning that day naming 2027-12-31

#### Scenario: Gap in loaded years
- **GIVEN** calendars are loaded for 2026 and 2028 but not 2027
- **WHEN** the current date is 2026-11-24
- **THEN** `calendarCoverageEndsOn` is 2026-12-31 and `calendarExpiryWarning` is true

#### Scenario: Current year not loaded
- **WHEN** the current year has no loaded calendar
- **THEN** `calendarCoverageEndsOn` is null and `calendarExpiryWarning` is true
