-- Whether an account may sign in (spec 12.1 field details are [P]).
-- Every request checks it, so deactivating an account locks it out at once. Nothing in the
-- application sets it to false except the owner's admin command (app/accounts/admin_cli.py).
ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;
