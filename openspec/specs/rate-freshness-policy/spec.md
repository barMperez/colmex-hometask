# rate-freshness-policy Specification

## Purpose

Decides whether a stored BoI representative rate observation is acceptable for use at a given moment. The decision follows BoI's actual publication calendar, and when the policy cannot be evaluated the system fails closed instead of guessing.

## Requirements

### Requirement: Business-day calendar
The system SHALL treat a date as a BoI publication business day only when it is Monday through Friday in the Asia/Jerusalem time zone and is not listed as a non-publication day.

#### Scenario: Weekend is not a business day
- **WHEN** the date is a Saturday or a Sunday
- **THEN** the system treats it as a non-business day

#### Scenario: Listed holiday is not a business day
- **GIVEN** 2026-10-27 (Tuesday) is listed as a non-publication day
- **WHEN** the system evaluates 2026-10-27
- **THEN** the system treats it as a non-business day

#### Scenario: Ordinary weekday is a business day
- **GIVEN** 2026-11-24 (Tuesday) is not listed as a non-publication day
- **WHEN** the system evaluates 2026-11-24
- **THEN** the system treats it as a business day

### Requirement: Publication deadline
For each business day the system SHALL compute a publication deadline, in Asia/Jerusalem local time: the expected publication time (15:15 Monday–Thursday, 12:15 Friday) plus the configured grace period.

#### Scenario: Weekday deadline
- **GIVEN** a grace period of 45 minutes
- **WHEN** the system computes the deadline for Tuesday 2026-11-24
- **THEN** the deadline is 2026-11-24 16:00 Asia/Jerusalem

#### Scenario: Friday deadline
- **GIVEN** a grace period of 45 minutes
- **WHEN** the system computes the deadline for Friday 2026-11-27
- **THEN** the deadline is 2026-11-27 13:00 Asia/Jerusalem

#### Scenario: Deadline respects daylight saving time
- **WHEN** the system computes deadlines for business days before and after an Israeli daylight-saving transition
- **THEN** each deadline is at the same local wall-clock time and the corresponding UTC instants differ by the offset change

### Requirement: Required publication date
At any evaluation instant, the system SHALL define the required publication date as the latest business day whose publication deadline is at or before that instant.

#### Scenario: Before today's deadline
- **GIVEN** a grace period of 45 minutes
- **WHEN** the evaluation instant is Tuesday 2026-11-24 10:00 Asia/Jerusalem
- **THEN** the required publication date is Monday 2026-11-23

#### Scenario: After today's deadline
- **GIVEN** a grace period of 45 minutes
- **WHEN** the evaluation instant is Tuesday 2026-11-24 16:01 Asia/Jerusalem
- **THEN** the required publication date is Tuesday 2026-11-24

#### Scenario: Weekend
- **WHEN** the evaluation instant is Sunday 2026-11-29 09:00 Asia/Jerusalem
- **THEN** the required publication date is Friday 2026-11-27

#### Scenario: Listed holiday
- **GIVEN** 2026-10-27 is listed as a non-publication day
- **WHEN** the evaluation instant is 2026-10-27 18:00 Asia/Jerusalem
- **THEN** the required publication date is Monday 2026-10-26

### Requirement: Eligibility evaluation
The system SHALL assess an observation as ELIGIBLE when its publication date is on or after the required publication date and no other rule in this capability makes it NOT_ELIGIBLE. Otherwise it SHALL assess it as NOT_ELIGIBLE with reason PUBLICATION_OVERDUE.

#### Scenario: Current rate is eligible
- **GIVEN** the required publication date is 2026-11-23
- **WHEN** the latest observation has publication date 2026-11-23
- **THEN** the assessment is ELIGIBLE

#### Scenario: Overdue rate is not eligible
- **GIVEN** the required publication date is 2026-11-24
- **WHEN** the latest observation has publication date 2026-11-23
- **THEN** the assessment is NOT_ELIGIBLE with reason PUBLICATION_OVERDUE
- **AND** the assessment includes the required publication date 2026-11-24

#### Scenario: Friday rate over the weekend
- **WHEN** the evaluation instant is Saturday 2026-11-28 and the latest observation has publication date Friday 2026-11-27
- **THEN** the assessment is ELIGIBLE

### Requirement: Maximum age cap
The system SHALL assess an observation as NOT_ELIGIBLE with reason MAX_AGE_EXCEEDED when its publication date is more than the configured maximum number of calendar days before the evaluation date, even if it satisfies the required publication date.

#### Scenario: Cap overrides calendar
- **GIVEN** a maximum age of 7 days
- **AND** the calendar data wrongly lists every day from 2026-11-17 to 2026-11-26 as non-publication days
- **WHEN** the evaluation date is 2026-11-26 and the latest observation is dated 2026-11-16
- **THEN** the assessment is NOT_ELIGIBLE with reason MAX_AGE_EXCEEDED

### Requirement: Fail closed when policy cannot be evaluated
The system SHALL refuse to produce an eligibility assessment, and SHALL report a policy-not-evaluable failure, when no active policy configuration is available or when the official non-publication calendar has not been loaded for any year the evaluation needs.

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

### Requirement: Future-dated observations are never eligible
The system SHALL assess an observation whose publication date is after the evaluation date in Asia/Jerusalem as NOT_ELIGIBLE with reason INVALID_PUBLICATION_DATE.

#### Scenario: Future-dated observation
- **WHEN** the evaluation date is 2026-11-24 and the observation's publication date is 2026-11-25
- **THEN** the assessment is NOT_ELIGIBLE with reason INVALID_PUBLICATION_DATE

### Requirement: Policy version traceability
Every eligibility assessment SHALL identify the policy version and the evaluation instant that produced it.

#### Scenario: Assessment carries policy version
- **GIVEN** active policy version 1
- **WHEN** an observation is assessed
- **THEN** the assessment includes policy version 1 and the evaluation instant in UTC

### Requirement: Operator-declared non-publication day
An operator SHALL be able to declare a date as a non-publication day, giving a reason and an actor. The declaration and its audit record SHALL be stored together, atomically, and take effect on the next evaluation without a restart.

#### Scenario: Declaring an unexpected missed publication restores eligibility
- **GIVEN** 2026-12-02 is a business day, its deadline has passed, and the latest observation is dated 2026-12-01
- **AND** the assessment is NOT_ELIGIBLE with reason PUBLICATION_OVERDUE
- **WHEN** an operator declares 2026-12-02 a non-publication day with a reason
- **THEN** the next assessment of the 2026-12-01 observation is ELIGIBLE
- **AND** an audit record exists with the actor, the reason, the declared date and the time of the declaration

#### Scenario: Declaration without a reason is rejected
- **WHEN** an operator attempts to declare a non-publication day without a reason
- **THEN** no non-publication day is stored and no audit record is written

#### Scenario: Unexpected missed publication without operator action stays not eligible
- **GIVEN** BoI did not publish on business day 2026-12-02 and no operator declaration exists
- **WHEN** the evaluation instant is after the 2026-12-02 deadline
- **THEN** an observation dated 2026-12-01 is NOT_ELIGIBLE with reason PUBLICATION_OVERDUE
