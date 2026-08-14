# company — specialized roles with gates

node ceo    skill ceo
node build  until --check 'pytest -q' implement the agreed change
node qa     skill qa
node mine   mine

edge ceo -> build
edge build -> qa
edge qa -> mine   on pass
edge qa -> build  on fail
edge qa -> ceo    on blocked
