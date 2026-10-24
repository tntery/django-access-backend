# Django Access Backend

Smart access control backend for managing mappings between access-control device IDs and external accounting-system users.

## Overview

This project provides:

- an admin-style UI for connecting and disconnecting access-device identities to accounting users
- an access-event API that decides whether access should be granted or rejected
- configurable authorization rules through a singleton settings model
- audit logging for access decisions
- a mock external accounting SQLite database for local development and testing

## Core Features

- Account mapping between `device_access_id` and `account_user_id`
- Pending mapping flow while the UI modal is open
- Authorization modes:
  - `grant_all`
  - `reject_all`
  - `check_balance`
- Access event logging via `AccessEventLog`
- Bootstrap command that seeds a mock external accounting database and initial mappings
- Tests for the critical decision and mapping flows

## Main Components

- [access/views.py](access/views.py)
  Handles the UI views, access-event API, modal state API, mapping API, external SQLite reads, and access-event logging.

- [access/models.py](access/models.py)
  Defines the mapping, pending mapping, access log, modal state, and singleton settings models.

- [access/management/commands/load_initial_data.py](access/management/commands/load_initial_data.py)
  Creates `external_accounting.sqlite3`, seeds external accounting users, and bootstraps `AccountMapping` rows.

- [access/management/seed_data.py](access/management/seed_data.py)
  Stores the initial internal mapping seed used during bootstrap.

- [access/tests.py](access/tests.py)
  Covers the critical paths in access decisions, mapping APIs, modal state handling, and bootstrap logic.

## Local Setup

### 1. Create and activate a virtual environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

At minimum, ensure these packages are installed:

```powershell
pip install django django-environ django-solo django-crispy-forms crispy-bootstrap5
```

### 3. Configure environment variables

Create a `.env` file in the project root with values similar to:

```env
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
```

### 4. Run migrations

```powershell
python manage.py migrate
```

### 5. Seed development data

```powershell
python manage.py load_initial_data
```

This command will:

- recreate `external_accounting.sqlite3`
- create the mock `accounting_users` table
- seed external accounting users
- bootstrap initial `AccountMapping` rows
- ensure `MappingModalState` exists

### 6. Start the server

```powershell
python manage.py runserver 0.0.0.0:8000
```

## Mock External Accounting Database

The file `external_accounting.sqlite3` is used to simulate an external accounting system.

Current external schema:

- `account_user_id`
- `full_name`
- `balance`

The external database intentionally does not store access-control mapping data. Device-to-account mappings remain in the application database through `AccountMapping`.

## Main Routes

UI:

- `/` - mapping management page
- `/mappings/<pk>/` - authenticated mapping detail page, opened by selecting a user's name
- `/settings/` - authorization settings page

### Mapping Details

Select a user's name on the User Connections page to view their account identifiers,
accounting system/group, balances, mapping timestamps, and all linked access IDs.
Each linked ID shows its authentication type, assigned device, location, configured
active/inactive status, and last recorded successful sync. These are stored values,
not live connection checks; a linked ID does not guarantee access authorization.

The access summary and newest-first event log show decisions attributed to that
mapping, including access ID, event type, device, and timestamp. Logs are paginated
at 25 events per page; summary counts cover the entire history. Events remain
associated with the mapping recorded at the time of the attempt, even if an ID is
subsequently disconnected or reassigned. Unattributed events are not matched by
access ID, to avoid showing another account's history.

### Mapping Access Overrides

Each mapping has an `access_override` value: **Auto** (the default), **Grant**, or
**Reject**. Staff and superusers can change it using the three-way control on the
detail page or in the list's Access decision column. Changes require browser
confirmation. Other authenticated operators can see the current decision but
cannot change it.

Grant and Reject are labeled **Manual** and take precedence over automatic
authorization rules and staff-ID exceptions for all linked access IDs, including
one-step mapping attempts. Auto restores existing automatic behavior. Inactive
devices still reject access. External user/balance syncs never modify this local
field.

Live decisions read the current mapping. Device cache downloads rebuild decisions
from current database state on every request, rather than trusting a process-local
snapshot that another worker cannot invalidate. The grouped decision snapshot in
`SystemMetadata` is also refreshed by UI override updates and device downloads. The device response
shape remains unchanged. Already-offline devices retain their downloaded decisions
until their next successful sync; overrides cannot reach them while offline.

Access logs store the **Auto/Manual** decision type at the time of each event.
Existing events are marked Auto by the migration.

After updating an existing installation, apply the new fields before restarting
the web and background workers:

```powershell
python manage.py migrate
```

API:

- `POST /api/access/<device_serial_number>` - submit an access event
- `GET /api/access/<device_serial_number>` - download decisions for device caching
- `POST /mappings/<pk>/access-override/` - staff-only, CSRF-protected override update (`access_override=auto|grant|reject`)
- `POST /api/modal-state` - set modal state (`open` or `closed`)
- `GET /api/mappings/pending` - fetch the pending scanned device ID
- `POST /api/mappings` - create or update a mapping
- `DELETE /api/mappings/<account_user_id>` - unmap a user

## Authorization Logic

For mappings set to Auto, the singleton `Setting` model controls authorization:

- `grant_all`: mapped users are granted access
- `reject_all`: mapped users are rejected
- `check_usd_balance`: check the USD balance against `usd_balance_threshold`
- `check_zwg_balance`: check the ZWG balance against `zwg_balance_threshold`
- `check_usd_or_zwg_balance`: either balance may meet its corresponding threshold

Manual mapping decisions take precedence over these rules. Unmapped IDs are
rejected unless they are configured staff-ID exceptions. The existing one-step
mapping flow grants valid mapping attempts in Auto mode.

## Access Logging

Each access decision is written to `AccessEventLog`, including:

- `device_access_id`
- `account_user_id` when a mapping exists
- `accounting_system`
- `access_status`
- `decision_type` (`auto` or `manual`)
- timestamp

## Running Tests

Run the critical-path test suite with:

```powershell
python manage.py test access.tests
```

Run the mapping override and detail-page tests with:

```powershell
python manage.py test access.test_overrides access.tests.AccountMappingDetailViewTests
```

The current tests cover:

- external accounting data loading and merge behavior
- access decision outcomes for all authorization modes
- modal-open rejection behavior
- mapping create/delete flows
- modal state cleanup behavior
- bootstrap command behavior and external DB schema

## Notes

- The project currently uses SQLite for both the app database and the mock external accounting database.
- The access-control side is modeled as incoming device events rather than direct user login.
- The UI is intended for admin/operators managing mappings and runtime settings.