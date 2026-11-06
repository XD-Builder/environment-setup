# Skill: tdd — red, then green, then refactor

Use when: you are about to edit production code for a feature, bugfix, or behavior change, including a task in an implementation plan.

Iron law: no production code without a failing test first. If you wrote the code before the test, delete it and start from the test. Do not keep it as a reference.

Exceptions, and only when the user said so: a throwaway spike, generated code, or a configuration file.

## Steps

1. Write one test for one behavior. Name the behavior. Assert on the real result, not on a mock, unless a mock is the only way to reach the behavior.
2. Run that test. Read the output. It must fail because the behavior is missing, not because of a typo or an error in the test. A test that passes immediately is testing something that already exists. Fix the test and run it again.
3. Write the smallest production change that makes that test pass. Do not add behavior the test does not ask for.
4. Run the test again and read the output. It must pass. Then run the project's suite, not only the new file. Name any failure, including one you did not cause.
5. Refactor only while the suite is green. Do not add behavior during refactor.
6. Repeat for the next behavior. A bugfix starts at step 1 with a test that reproduces the bug.

## Report

- Red: command and the failure you read
- Green: command and the pass you read
- Suite: command and result
- Left: anything not covered

## Rationalizations

- "Too simple to test." Simple code breaks. Write the test.
- "I'll test after." A test you never watched fail does not prove it catches the bug.
- "Keep the code I already wrote and add tests." That is testing after. Delete the code.
- "The suite can wait until the end." The end-of-task run does not tell you which step broke it.
