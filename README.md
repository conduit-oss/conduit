# Conduit

Conduit flags the lines in your code when an outside service or SDK you depend on changes.

## Install

```bash
git clone https://github.com/conduit-oss/conduit.git
cd conduit
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ./conduit
```

On Windows, activate with `.venv\Scripts\activate`.

Check that the commands are there:

```bash
conduit --help
```

You should see `map`, `watch`, `impact`, and `service`.

## Quickstart

Map a small app that talks to a database, cloud storage, a webhook, and one API client. From the repo root:

```bash
conduit map --path examples/service-clients
```

The report starts like this. `root` is the absolute path of the folder you passed. The same kind of block is printed for `boto3` and `stripe`, and a limitations list follows.

```text
Outside-in service map
root: /workspace/examples/service-clients
services: sqlalchemy, boto3, stripe

sqlalchemy
- db.py:3  db_alias  sqlalchemy.orm.Query  [high]
- db.py:3  db_alias  sqlalchemy.orm.Session  [high]
- db.py:11  db_call  sqlalchemy.orm.Query  [high]
- db.py:12  db_call  sqlalchemy.orm.Query.from_self  [high]
- db.py:16  db_constructor  sqlalchemy.orm.Session  [high]
- db.py:17  db_call  sqlalchemy.orm.Session.query  [high]

Reachability

- db.py:12  by_pk  query.from_self  (direct, high)
  path: db.py:12 by_pk query.from_self
```

`services` are the outside services this repo actually talks to. Each bullet is a file, a line, and how that line uses the service. Reachability is your call, and the path back to the service. `[high]` on a direct call means Conduit could see the call itself.

## map

`conduit map` lists those services and the lines that reach them, including through a wrapper or an alias. It does not edit anything.

```bash
conduit map --path examples/service-clients --service sqlalchemy
```

| Flag | Meaning |
|------|---------|
| `--path` | Folder to read. Default is the current directory. |
| `--service` | Only this service. |
| `--markdown` | Same report, with the evidence path (`sqlalchemy -> db-api`). |
| `--json` | The same facts as JSON. |

## watch

`conduit watch` looks at a newer release of each service on the map and prints the lines that release breaks. A version number by itself is not a result.

Omit `--feeds` and Conduit reads each package's public project page. It follows the source repository linked there, then reads the release feed, a changelog file at those release tags, and the API spec at both tags. npm is included only when that page links to it. Responses are cached. If a file cannot be fetched, the report says `not compared` and why.

This repo includes recorded copies, so the example does not use the network:

```bash
conduit watch --path examples/service-clients --feeds conduit/fixtures/feeds
```

```text
sqlalchemy 1.4.52 -> 2.1.4
  Query.from_self removed [high]
    db.py:12  query.from_self [high]
skipped: stripe: no public feeds configured
```

The first line is the package and the two versions. Under it, each change, then `file:line` and the call. `[high]` is a direct break. `[low]` is weaker: a deprecation, or a caller of a wrapper. `--show-callers` lists those callers. The default is a count. `boto3` is not listed: its published library and the service model shipped in its dependency were compared, and no call in this app was hit. `stripe` is on the map, but this app does not pin a version, so the recorded feeds have nothing to read.

| Flag | Meaning |
|------|---------|
| `--path` | Folder to read. |
| `--feeds` | Recorded copies of public release info. Omit it to discover the same kinds of feeds from the project page and source repository. |
| `--service` | Only this service. |
| `--show-callers` | List wrapper callers under each breaking line. |
| `--json` | Machine-readable report. |
| `--out` | Also write the report files into this directory. |

## impact

`conduit impact` is the same line check for one service.

```bash
conduit impact --dep sqlalchemy --path examples/service-clients --fixtures conduit/fixtures/impact
```

That prints the SQLAlchemy block from the watch example: `Query.from_self` removed at `db.py:12`.

`--fixtures` points at recorded copies in this repo (old and new libraries, cloud models, webhook schemas). Omit it to download the public copies. `--from` and `--to` pick the two versions when the project has no pin, or you want a specific pair.

| Flag | Meaning |
|------|---------|
| `--dep` | Required. The service or its client package. |
| `--path` | Folder to read. |
| `--from` / `--to` | Versions to compare. |
| `--fixtures` | Recorded copies. Omit to use the network. |
| `--show-callers` | List wrapper callers. |
| `--json` | Machine-readable report. |

If Conduit cannot tell which versions to compare, or cannot find two API specs, it says `not compared` and why. That is not an all-clear.

## service

`conduit service` saves a map for one customer, then checks it later and writes a local note of lines that break.

```bash
conduit service add --customer acme --path examples/service-clients --store ./.conduit-service
conduit service tick --store ./.conduit-service --feeds conduit/fixtures/feeds
```

`add` prints `Stored map for acme at ./.conduit-service/customers/acme` (as an absolute path). `tick` checks every saved map in `--store`. `--customer` limits that check to one id. `--feeds` is the same recorded copies as watch. Omit it to discover feeds from each project's public page.

## How detection works

Conduit does not keep a list of product names.

A dependency counts as an outside service only when both of these are true:

- Its own package shows network or database I/O, or it ships a versioned API spec.
- Your code builds a client, a session, or a connection.

A library that only computes is skipped. A generic HTTP library is not itself an outside service. It is how a raw URL call is recognized.

To see what changed, Conduit compares the old and new published libraries. Versioned spec files shipped inside the package, or inside a dependency of that package, are compared too and matched to the calls that use them. When the package's source repository has an API spec at both release tags, those two specs are compared as well. Specs are found by reading the published packages and by walking the source repository at the two tags. If there is no spec history, the report says `not compared` and why.

`conduit map --markdown` prints the evidence path, such as `sqlalchemy -> db-api`.

## Limits and FAQ

Python only. Conduit does not bump lockfiles, open dependency-update pull requests, or scan for security advisories. It does not watch calls between your own services. It does not edit your tree. It prints the lines.

These calls stay invisible:

- A name passed through a variable into `getattr`, or a client built by a dependency-injection container.
- Positional arguments. They are not matched to parameter names.
- A cloud service chosen by a variable. It is not tied to an operation.
- Webhook fields read in a helper in another file.

**Why was my library skipped?** Its package does not show network or database I/O and does not ship a versioned API spec, or your code never builds a client from it. `conduit impact --dep pydantic --path examples/service-clients` exits with an error: pydantic is not an outside service.

**What does `not compared` mean?** The call was seen, but Conduit could not pick two versions, or could not find an API spec at both release tags. The note says which.

**Where are the recorded examples?** `examples/service-clients` is the app. `conduit/fixtures/feeds` and `conduit/fixtures/impact` are offline snapshots of public releases. Rebuild them with `python scripts/record_fixtures.py`.

Scored checks against real migrations are in [docs/backtest.md](docs/backtest.md). GitHub Actions notes are in [docs/github-actions.md](docs/github-actions.md).

## License

Apache-2.0
