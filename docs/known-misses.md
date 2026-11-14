# Known misses

Some breaking changes Conduit will find and flag as **needs-human**, but will not try to fix on its own. It reports the line and the reason, and leaves the change to you. This page lists those cases and why, in plain terms.

## Your class is built on top of a class the new SDK removed

**Example:** gptcache has classes that subclass `openai.ChatCompletion`. That class was removed in openai 1.x.

**What Conduit does:** it flags these classes as needs-human. It does not rewrite the inheritance and does not swap in a new base class.

**Why:** fixing this means three things at once. Conduit has to notice that your class inherits from the SDK class, know that the parent class was dropped in the new version, and find something in the new SDK that does the same job. Often nothing is a direct replacement, because the SDK changed its design instead of renaming a class. That is specialized work Conduit does not do yet, and a wrong guess here would break every method on your class, so it hands the case to a person.

## A removed name is still used after the fix

**Example:** a file imports a type that the new SDK removed and uses it in several places, not just in the call Conduit rewrote.

**What Conduit does:** if the old name is no longer used after the rewrite, Conduit drops the import. If the name is still used somewhere else in the file, the site stays needs-human.

**Why:** deleting an import that other code still needs would break that code. Choosing a replacement for every other use is a bigger change than the one Conduit was asked to make.

## Conduit can't tell where an object came from

**Example:** code calls `self.index.query(...)`, but `self.index` is set somewhere Conduit can't follow, such as from a config value, a container, or a helper that returns different things.

**What Conduit does:** it flags the call as needs-human.

**Why:** to rewrite a method call safely, Conduit has to know which class the object is in the new SDK. If it can't trace the object back to where it was created, any rewrite would be a guess.
