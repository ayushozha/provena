# Provena PM PRD Template

Use this template whenever new Provena work is added to `loop/prd.json`.

## Epic

- `epicId`
- `epicTitle`
- `epicDescription`
- `successMetrics`

## Feature

- `featureId`
- `featureTitle`
- `featureDescription`
- `expectedBehaviors`
- `dependencies`

## User Story

- `id`
- `title`
- `priority`
- `persona`
- `userStory`
- `problemStatement`
- `featureDescription`
- `expectedBehaviors`
- `acceptanceCriteria`
- `verificationCommands`
- `uiTestFocus`
- `sentryChecks`

## Writing style

Write this like a product manager:

- clearly identify the user or operator persona
- state the problem and why it matters
- describe the feature behavior in plain product language
- make acceptance criteria observable and testable
- keep stories small enough for one implementation iteration

## QA handoff

Every story must be QA-ready after implementation. That means:

- `uiTestFocus` should list affected screens or user journeys when relevant
- `sentryChecks` should list the runtime risks to watch in Sentry
- acceptance criteria should be concrete enough for a QA agent to derive test cases
