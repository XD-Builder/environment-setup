# Docket keeps everything in plain files on purpose — commit them.
# If a loop's Memory section holds things you don't want in git, move the
# loop here and ignore it explicitly, e.g.:
# loops/private-*.loop.md
#
# One exception worth ignoring: record.jsonl.lock is a transient lock held
# while an entry is appended (so parallel agents can't break the chain).
# It is deleted on release and should never be committed.
