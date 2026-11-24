# Spec Delta

## MODIFIED Requirements

### Requirement: Operator-declared non-publication day
An operator SHALL be able to declare a date as a non-publication day, giving a reason and an actor. Without an explicit force flag, the date MUST be between today and `max_age_days` days before today in Asia/Jerusalem. The declaration and its audit record, which records whether it was forced, SHALL be stored together atomically and take effect on the next evaluation without a restart.

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

#### Scenario: Future date refused
- **GIVEN** today is 2026-12-02 in Asia/Jerusalem
- **WHEN** an operator declares 2026-12-03 without the force flag
- **THEN** nothing is stored, no audit record is written, and the refusal names the allowed range 2026-11-25 to 2026-12-02

#### Scenario: Date older than the max-age window refused
- **GIVEN** today is 2026-12-02 and the active policy's maximum age is 7 days
- **WHEN** an operator declares 2026-11-24 without the force flag
- **THEN** nothing is stored and no audit record is written

#### Scenario: Forced declaration outside the window
- **WHEN** an operator declares 2026-12-24 with the force flag and a reason
- **THEN** the day is stored and its audit record marks the declaration as forced

## ADDED Requirements

### Requirement: Retracting an operator declaration
An operator SHALL be able to retract a non-publication day that was declared by an operator, giving a reason and an actor. The removal and an audit record containing the removed declaration SHALL be stored atomically. Days that come from the official BoI calendar SHALL NOT be retractable.

#### Scenario: Retracting a mistaken declaration
- **GIVEN** an operator declared 2026-12-02 a non-publication day
- **WHEN** an operator retracts 2026-12-02 with a reason
- **THEN** 2026-12-02 is a business day again at the next evaluation
- **AND** an audit record holds the actor, the reason and the removed declaration

#### Scenario: Official calendar day cannot be retracted
- **WHEN** an operator tries to retract 2026-10-27, which comes from the BoI calendar
- **THEN** nothing changes and no audit record is written

#### Scenario: Nothing to retract
- **WHEN** an operator tries to retract a date that is not a non-publication day
- **THEN** nothing changes and no audit record is written

### Requirement: Policy version activation
An operator SHALL be able to activate a new policy version, giving its grace period, maximum age, weekday and Friday publication times, a reason and an actor. The new version SHALL be the previous highest version plus one and keep the current time zone. The new version, the deactivation of the previous one and an audit record with both SHALL be stored atomically, and take effect on the next evaluation.

#### Scenario: Activating a stricter grace period
- **GIVEN** policy version 1 with a grace period of 45 minutes is active
- **WHEN** an operator activates a policy with a grace period of 30 minutes and the other values unchanged
- **THEN** version 2 is the only active policy and the next weekday deadline is 15:45 Asia/Jerusalem
- **AND** the next assessment reports policy version 2
- **AND** an audit record holds the actor, the reason, and versions 1 and 2

#### Scenario: Invalid policy values rejected
- **WHEN** an operator activates a policy with a negative grace period or a maximum age below 1 day
- **THEN** nothing changes and no audit record is written
