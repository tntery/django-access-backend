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
- `/settings/` - authorization settings page

API:

- `POST /api/access` - submit an access event
- `POST /api/modal-state` - set modal state (`open` or `closed`)
- `GET /api/mappings/pending` - fetch the pending scanned device ID
- `POST /api/mappings` - create or update a mapping
- `DELETE /api/mappings/<account_user_id>` - unmap a user

## Authorization Logic

The singleton `Setting` model controls how access decisions are made:

- `grant_all`: every access event is granted
- `reject_all`: every access event is rejected
- `check_balance`: access is granted only when the mapped user balance is greater than or equal to `balance_threshold`

When the mapping modal is open, incoming access events are always rejected and captured as pending mapping candidates.

## Access Logging

Each access decision is written to `AccessEventLog`, including:

- `device_access_id`
- `account_user_id` when a mapping exists
- `accounting_system`
- `access_status`
- timestamp

## Running Tests

Run the critical-path test suite with:

```powershell
python manage.py test access.tests
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