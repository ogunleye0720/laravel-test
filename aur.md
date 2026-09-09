# Connecting to Aurora PostgreSQL with IAM authentication

This guide explains how an application running on Amazon EKS, Lambda, ECS, or
EC2 connects to Aurora PostgreSQL without storing a database password.

**Audience:** Application developers and service owners using an Aurora
PostgreSQL cluster managed by the platform team.

> [!IMPORTANT]
> IAM authentication replaces the database password only during connection
> establishment. PostgreSQL roles and grants still determine what the user can
> do after connecting.

## Quick start

1. Obtain `rds-db:connect` permission for your workload's IAM role.
2. Confirm that the platform-provisioned PostgreSQL user is `app`.
3. Confirm network access to the cluster on TCP port `5432`.
4. Generate an IAM authentication token for each new physical database
   connection, or use an AWS Advanced Wrapper that manages token refresh.
5. Use the token as the PostgreSQL password and connect over TLS.
6. Test the IAM connection before removing the application's existing database
   secret. Keep a tested rollback path during the migration window.

The database host, port, database name, SQL, ORM, and transaction behaviour do
not otherwise change.

## How it works

An IAM-authenticated connection has four independent requirements:

1. **AWS identity:** The workload obtains temporary AWS credentials through the
   SDK's default credential provider chain.
2. **IAM authorization:** That identity is allowed to call `rds-db:connect` for
   the cluster resource ID and PostgreSQL user.
3. **Database authentication:** The PostgreSQL role exists and has been granted
   `rds_iam`.
4. **Database authorization:** The PostgreSQL role has the database, schema,
   table, sequence, and function privileges required by the application.

At connection time:

```text
Workload IAM credentials
          |
          v
AWS SDK creates a SigV4 authentication token
          |
          v
Application sends the token in PostgreSQL's password field over TLS
          |
          v
RDS validates the signature and rds-db:connect permission
          |
          v
PostgreSQL applies the app role's normal database privileges
```

The token is valid for **15 minutes for establishing a connection**. It does
not terminate a session after 15 minutes; an established connection remains
usable until it is closed or otherwise interrupted. See the
[AWS IAM database authentication guide](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html).

Creating the token is a local SigV4 signing operation and does not call an RDS
API. However, the SDK may need network access to obtain or refresh the
workload's underlying AWS credentials, for example through EKS Pod Identity,
IRSA/STS, ECS task metadata, or EC2 IMDS.

## Workload identity

Do not put static AWS access keys in application configuration. Use the native
workload identity for the compute platform.

| Compute platform | Recommended identity | Notes |
|---|---|---|
| EKS | EKS Pod Identity or IAM Roles for Service Accounts (IRSA) | Use a dedicated IAM role and Kubernetes service account per application. Do not rely on the node role unless the platform explicitly provides node-wide access. |
| Lambda | Lambda execution role | The function also needs VPC connectivity to a private Aurora cluster. |
| ECS | ECS task role | Use the task role, not the container-instance role. |
| EC2 | Instance profile | The SDK retrieves credentials through IMDSv2. |

AWS recommends a dedicated application role with EKS Pod Identity or IRSA and
restricting pod access to node credentials. See
[EKS identity and access management best practices](https://docs.aws.amazon.com/eks/latest/best-practices/identity-and-access-management.html).

## 1. Get IAM access

The platform has already created the PostgreSQL role used for IAM
authentication. The self-service step below grants an AWS workload permission
to connect as that existing database role; it does not run `CREATE USER` or
change database grants.

### Self-service access module

Call the `access` module from the consuming service's Terraform:

```hcl
data "aws_rds_cluster" "target" {
  cluster_identifier = "dev-example-right-mastodon"
}

module "db_access" {
  source = "git@github.com:OnScale/onscale-terraform-aurora.git//modules/access?ref=vX.Y.Z"

  name                  = "my-service"
  cluster_resource_id   = data.aws_rds_cluster.target.cluster_resource_id
  db_connect_role_names = [aws_iam_role.my_service.name]
}
```

> [!CAUTION]
> `name` must be unique for each module caller. Reusing a name can cause an
> `EntityAlreadyExists` error. Use the service name or another stable,
> repository-specific identifier.

### Attach an exported policy

If the database configuration already exports a suitable policy ARN, attach it
to the workload role:

```hcl
resource "aws_iam_role_policy_attachment" "database" {
  role       = aws_iam_role.my_service.name
  policy_arn = var.db_connect_policy_arn
}
```

### Understand the policy resource

An IAM database authentication policy targets an RDS database resource ID and
a PostgreSQL username:

```text
arn:aws:rds-db:us-east-1:123456789012:dbuser:cluster-ABC123/app
                                             |             |
                                             |             +-- PostgreSQL user
                                             +---------------- cluster resource ID
```

The IAM principal is determined by the role to which the policy is attached,
not by the resource ARN. Multiple workload roles can therefore be authorized
to connect as the same PostgreSQL user. See
[Creating an IAM policy for database access](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.IAMPolicy.html).

### Cross-account access

The normal platform workflow assumes that the workload and database are in the
same AWS account. AWS supports cross-account IAM database authentication by
creating an authorized role in the database account and allowing the workload
account to assume it. Contact the platform team before using that pattern; the
self-service module may not configure the required trust relationship.

## 2. Confirm network and connection details

IAM authorization does not provide network connectivity. The workload must be
able to resolve the cluster endpoint and reach it on TCP port `5432`.

Workloads in the approved private subnets are normally covered by the cluster
security-group rules. For a workload elsewhere, confirm routing, DNS, and the
source security-group or CIDR rules with the platform team.

Use the database repository outputs or platform-provided values:

| Setting | Example | Notes |
|---|---|---|
| `PGHOST` | `dev-example.cluster-abc.us-east-1.rds.amazonaws.com` | Use the writer endpoint for read/write traffic. |
| Reader host | `dev-example.cluster-ro-abc.us-east-1.rds.amazonaws.com` | Use only for read-only traffic. |
| `PGPORT` | `5432` | The token is signed for a host and port. |
| `PGDATABASE` | `prompt_yeti` | PostgreSQL database name. |
| `PGUSER` | `app` | Current platform-provisioned IAM database user. |
| `PGSSLMODE` | `verify-full` | Encrypts the connection and verifies the server certificate and hostname. |
| `PGSSLROOTCERT` | `/etc/ssl/certs/aws-rds-global-bundle.pem` | Path to the trusted Amazon RDS CA bundle. |
| `AWS_REGION` | `us-east-1` | Region containing the cluster. |

Download and package the current CA bundle using the
[Amazon RDS certificate guidance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html).

> [!IMPORTANT]
> Generate the token for the real RDS endpoint. AWS does not support using a
> custom Route 53 name in place of the RDS endpoint when generating the token.
> Using the real endpoint for both token generation and the connection is the
> simplest and safest configuration.

## 3. Connect from Python

### Option A: Boto3 and Psycopg

This is suitable for one-shot jobs or applications whose connection-pool
integration can generate credentials whenever it creates a new physical
connection.

```python
import os

import boto3
import psycopg


rds = boto3.client("rds", region_name=os.environ["AWS_REGION"])


def connect():
    host = os.environ["PGHOST"]
    port = int(os.environ.get("PGPORT", "5432"))
    username = os.environ["PGUSER"]

    token = rds.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=username,
    )

    return psycopg.connect(
        host=host,
        port=port,
        dbname=os.environ["PGDATABASE"],
        user=username,
        password=token,
        sslmode=os.environ.get("PGSSLMODE", "verify-full"),
        sslrootcert=os.environ["PGSSLROOTCERT"],
        connect_timeout=10,
    )
```

Do not generate one token at process startup and configure it as a permanent
pool password. A pool can open replacement connections long after that token
has expired.

### Option B: AWS Advanced Python Wrapper

The AWS Advanced Python Wrapper can generate and cache IAM tokens and add
Aurora-aware failover handling:

```python
import os

from aws_advanced_python_wrapper import AwsWrapperConnection
from psycopg import Connection


def connect():
    return AwsWrapperConnection.connect(
        Connection.connect,
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        sslmode=os.environ.get("PGSSLMODE", "verify-full"),
        sslrootcert=os.environ["PGSSLROOTCERT"],
        plugins="iam,failover",
        wrapper_dialect="aurora-pg",
        iam_region=os.environ["AWS_REGION"],
        connect_timeout=10,
    )
```

The call shape is significant: pass Psycopg's `Connection.connect` function to
`AwsWrapperConnection.connect`. There is no global driver-registration step.

Use an organization-approved and pinned wrapper release. Do not use AWS
Advanced Python Wrapper versions earlier than `1.4.0`; those releases are
affected by a published privilege-escalation vulnerability. At the time this
page was updated, the current PyPI release was `3.0.0`. Review the
[AWS Advanced Python Wrapper IAM plugin documentation](https://github.com/aws/aws-advanced-python-wrapper/blob/main/docs/using-the-python-wrapper/using-plugins/UsingTheIamAuthenticationPlugin.md)
and [package releases](https://pypi.org/project/aws-advanced-python-wrapper/).

## 4. Connect from Java with Spring Boot and HikariCP

Do not set a generated IAM token as a static `spring.datasource.password`.
HikariCP can create replacement connections after the token expires and then
reuse the stale value.

Use the AWS Advanced JDBC Wrapper IAM plugin so a valid token is supplied when
the pool opens a physical connection.

### Dependencies

Pin approved versions through the service's dependency-management mechanism.
The IAM plugin also requires the AWS SDK for Java v2 RDS module at runtime.

```xml
<dependency>
  <groupId>software.amazon.jdbc</groupId>
  <artifactId>aws-advanced-jdbc-wrapper</artifactId>
  <version>4.4.0</version>
</dependency>

<dependency>
  <groupId>software.amazon.awssdk</groupId>
  <artifactId>rds</artifactId>
</dependency>

<dependency>
  <groupId>org.postgresql</groupId>
  <artifactId>postgresql</artifactId>
</dependency>
```

Check [Maven Central](https://central.sonatype.com/artifact/software.amazon.jdbc/aws-advanced-jdbc-wrapper)
and the project's security notices before upgrading.

### Spring configuration

```yaml
spring:
  datasource:
    url: jdbc:aws-wrapper:postgresql://${PGHOST}:${PGPORT}/${PGDATABASE}
    username: ${PGUSER}
    driver-class-name: software.amazon.jdbc.Driver
    # No password property.
    hikari:
      exception-override-class-name: software.amazon.jdbc.util.HikariCPSQLException
      data-source-properties:
        wrapperPlugins: iam,auroraConnectionTracker,failover2,efm2
        wrapperDialect: aurora-pg
        iamRegion: ${AWS_REGION}
        sslmode: verify-full
        sslrootcert: ${PGSSLROOTCERT}
```

Common mistakes:

- Use the `jdbc:aws-wrapper:postgresql://` URL prefix. A normal
  `jdbc:postgresql://` URL bypasses the wrapper.
- Use `software.amazon.jdbc.Driver`, not `org.postgresql.Driver`.
- Put wrapper parameters under
  `spring.datasource.hikari.data-source-properties`.
- Include the `iam` plugin. Specifying `wrapperPlugins` replaces the default
  plugin list, so list every plugin the application needs.
- Confirm plugin names against the pinned wrapper version. Current releases use
  `failover2` and `efm2`; older releases and examples may use `failover` or
  `efm`.

See the AWS wrapper's
[Spring and HikariCP configuration](https://github.com/aws/aws-advanced-jdbc-wrapper/blob/main/docs/using-the-jdbc-driver/UsingTheJdbcDriver.md)
and [IAM plugin documentation](https://github.com/aws/aws-advanced-jdbc-wrapper/blob/main/docs/using-the-jdbc-driver/using-plugins/UsingTheIamAuthenticationPlugin.md).

### Manual Java token generation

Manual token generation is suitable for a one-shot job. Integrate token
generation with the pool's physical-connection creation path before using it in
a long-running service.

```java
RdsClient rds = RdsClient.builder()
    .region(Region.of(System.getenv("AWS_REGION")))
    .build();

RdsUtilities utilities = rds.utilities();

String token = utilities.generateAuthenticationToken(builder -> builder
    .hostname(System.getenv("PGHOST"))
    .port(Integer.parseInt(System.getenv("PGPORT")))
    .username(System.getenv("PGUSER")));
```

Close the `RdsClient` when the application shuts down.

## 5. Other languages

| Language | Package or API |
|---|---|
| Python | Boto3 `rds.generate_db_auth_token` |
| Java | AWS SDK for Java v2 `RdsUtilities.generateAuthenticationToken` |
| Node.js | `@aws-sdk/rds-signer` |
| Go | `github.com/aws/aws-sdk-go-v2/feature/rds/auth` |
| AWS CLI | `aws rds generate-db-auth-token` |

The pattern is the same: use automatically refreshed workload credentials to
generate a token, then pass the complete token as the database password. IAM
tokens are typically at least 1 KiB and can be larger; make sure configuration,
drivers, and proxies do not truncate them.

## 6. Connection pooling and capacity

The token lifetime applies to authentication, not to the duration of an open
database session.

Follow these rules:

- Generate or obtain a valid token whenever the pool establishes a new
  physical connection.
- Do not generate a token for every SQL statement or application request.
- Do not keep a startup-generated token as the pool's static password.
- Reuse healthy connections instead of creating a connection per request.
- Configure sensible pool size, connection timeout, idle timeout, and maximum
  lifetime values for the workload.
- Monitor IAM authentication failures and throttling.

IAM authentication consumes additional database resources. AWS documents an
additional memory requirement of approximately 300–1000 MiB and can throttle
bursts of new IAM-authenticated connections. For high connection churn or
unpredictable bursts, evaluate
[Amazon RDS Proxy](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy.html).

## 7. Migrate an existing application safely

Applications currently using the Aurora master username and password should
move to the separate IAM database user `app`. Do **not** grant `rds_iam` to the
master user: for PostgreSQL, IAM authentication takes precedence over password
authentication once `rds_iam` is granted, removing the password-based
break-glass path for that user.

Use this rollout sequence:

1. Confirm that IAM authentication is enabled on the cluster and that the `app`
   PostgreSQL role exists with the required database privileges.
2. Grant the workload role `rds-db:connect` for `app`.
3. Deploy application support for IAM tokens while retaining the current
   password configuration as a rollback option.
4. Test from the real workload environment, including normal reads, writes,
   transactions, sequence-backed inserts, and migrations if the application
   runs them.
5. Switch the application connection username from the master user to `app`.
6. Monitor connection success, authorization errors, pool churn, and IAM
   authentication metrics.
7. After the agreed rollback window, remove the master credential from the
   application configuration and rotate the master password.

The master credential remains in the platform's approved secret store for
break-glass administration. It must not remain available to the application.

## 8. Test manually

Run the test from a network location that can reach the cluster:

```bash
export PGHOST="dev-example.cluster-abc.us-east-1.rds.amazonaws.com"
export PGPORT="5432"
export PGDATABASE="prompt_yeti"
export PGUSER="app"
export AWS_REGION="us-east-1"
export PGSSLROOTCERT="/etc/ssl/certs/aws-rds-global-bundle.pem"

export PGPASSWORD="$(aws rds generate-db-auth-token \
  --hostname "$PGHOST" \
  --port "$PGPORT" \
  --username "$PGUSER" \
  --region "$AWS_REGION")"

psql "host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER \
sslmode=verify-full sslrootcert=$PGSSLROOTCERT"
```

Token generation does not prove that the IAM policy or PostgreSQL role is
correct; authorization is checked when the database connection is attempted.

### Testing through an SSM port-forwarding tunnel

Keep the real RDS hostname for token generation and certificate verification,
but direct the TCP connection to the local tunnel with `hostaddr`:

```bash
export RDSHOST="dev-example.cluster-abc.us-east-1.rds.amazonaws.com"
export LOCALPORT="15432"
export PGUSER="app"
export AWS_REGION="us-east-1"

export PGPASSWORD="$(aws rds generate-db-auth-token \
  --hostname "$RDSHOST" \
  --port 5432 \
  --username "$PGUSER" \
  --region "$AWS_REGION")"

psql "host=$RDSHOST hostaddr=127.0.0.1 port=$LOCALPORT \
dbname=prompt_yeti user=$PGUSER sslmode=verify-full \
sslrootcert=/etc/ssl/certs/aws-rds-global-bundle.pem"
```

This preserves hostname verification instead of weakening TLS to
`sslmode=require` merely because the TCP connection is tunnelled through
localhost.

## 9. Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| `PAM authentication failed`, `NotAuthorized`, or insufficient-permission metric | Wrong IAM identity, missing `rds-db:connect`, incorrect cluster resource ID, or wrong database username | Run `aws sts get-caller-identity`; compare the policy resource with the cluster resource ID and `PGUSER`. |
| Invalid or malformed token | Token truncated, incorrectly quoted, generated for the wrong host/port/region, or custom hostname used | Confirm that the full token reaches the driver and use the real RDS endpoint. |
| Authentication fails about 15 minutes after startup | The pool cached a startup-generated token | Generate a token when each physical connection is opened or use an AWS wrapper. |
| Connection timeout | Network path, security group, network ACL, routing, or DNS | Test name resolution and TCP reachability from the workload environment. |
| `could not translate host name` | DNS resolution or VPC DNS configuration | Resolve the RDS endpoint from the same pod, task, function, or instance. |
| `connection refused` | Wrong endpoint/port or unavailable target | Confirm endpoint type, port, and cluster status. |
| TLS hostname or certificate failure | Custom alias, missing/outdated RDS CA bundle, or incorrect `sslrootcert` | Connect using the real RDS endpoint and current CA bundle. |
| Works from one pod but not another | Different service accounts, Pod Identity associations, IRSA annotations, or node-level fallback credentials | Compare `aws sts get-caller-identity` and pod identity configuration in both pods. |
| IAM authentication throttling | Too many new physical connections | Reduce connection churn, tune the pool, and consider RDS Proxy. |
| Connected but `permission denied` for a table/schema/sequence | IAM authentication succeeded; PostgreSQL authorization is missing | Inspect database grants and object ownership. This is not an IAM policy problem. |

### Checks that resolve most failures

```bash
# 1. Verify the AWS identity used by the workload.
aws sts get-caller-identity

# 2. Verify DNS from the workload environment.
getent hosts "$PGHOST"

# 3. Verify TCP reachability.
nc -vz "$PGHOST" "$PGPORT"

# 4. Generate a token. This validates local inputs and credential availability,
#    but does not validate rds-db:connect authorization.
aws rds generate-db-auth-token \
  --hostname "$PGHOST" \
  --port "$PGPORT" \
  --username "$PGUSER" \
  --region "$AWS_REGION"
```

For EKS, verify that the pod uses its intended service account and Pod Identity
or IRSA association. Do not assume the node role is the caller.

### Observability

`generate-db-auth-token` is not an RDS API call and is not recorded as a
CloudTrail event. RDS provides near-real-time CloudWatch metrics including:

- `IamDbAuthConnectionRequests`
- `IamDbAuthConnectionSuccess`
- `IamDbAuthConnectionFailure`
- `IamDbAuthConnectionFailureInvalidToken`
- `IamDbAuthConnectionFailureInsufficientPermissions`
- `IamDbAuthConnectionFailureThrottling`
- `IamDbAuthConnectionFailureServerError`

RDS can also export the `iam-db-auth-error` log to CloudWatch Logs. See
[Troubleshooting IAM database authentication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.Troubleshooting.html).

Never log an authentication token. Treat it as a short-lived credential.

## 10. Security and platform considerations

### Shared database identity

The current platform model allows multiple services to connect as the same
PostgreSQL user, `app`. PostgreSQL views such as `pg_stat_activity` identify the
database user, not the originating IAM role, so database-level attribution
between services is limited.

Use application-level connection metadata, structured logs, and workload IAM
telemetry where available. Request a dedicated PostgreSQL role if the service
requires stronger database-level isolation or auditability.

### Administrative privileges

The current `app` role is an administrative role with `rds_superuser` and broad
rights on the database and `public` schema. This is a platform contract, not a
requirement of IAM authentication. Services needing least-privilege access
should use a separately designed database role and grants.

### Master credentials

The Aurora master user remains password-authenticated for platform bootstrap
and break-glass administration. Applications must not use or receive the master
credential after migration. Granting `rds_iam` to the PostgreSQL master user
would make IAM authentication take precedence over its password.

## FAQ

### Do I need to rotate an application database password?

No application database password exists after migration. The workload's AWS
credentials are temporary and refreshed by the platform credential provider.
The separate Aurora master password still follows the platform's rotation and
break-glass process.

### Does the application need to reconnect every 15 minutes?

No. The token is checked only while establishing a connection. Existing
sessions are not terminated when the token expires.

### Can the same token be reused?

It can be reused only while valid, but generating or retrieving a valid token
for each new physical connection is simpler and safer. Do not use a token older
than 15 minutes.

### Can I use the reader endpoint?

Yes. Generate the token for the reader endpoint and connect to that same
endpoint. Reader connections must be treated as read-only.

### Can I use a custom DNS name?

Use the real RDS endpoint by default. AWS does not allow a custom Route 53 name
to replace the RDS endpoint during token generation. Some AWS wrappers support
an `iamHost`/`iam_host` override for custom connection endpoints, but TLS and
failover settings must also be configured correctly.

### Can I continue using the master password?

The master password is retained only for platform bootstrap and break-glass
administration. It is not an application credential.

### Is cross-account access supported?

AWS supports it through role assumption into the database account. The current
platform self-service workflow may not provision that pattern automatically;
contact the platform team.

### What about DocumentDB?

DocumentDB uses a different IAM authentication and authorization model and is
outside the scope of this guide.

## References

- [AWS: IAM database authentication for RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html)
- [AWS: Connecting with an IAM authentication token](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.Connecting.html)
- [AWS: IAM policy for database access](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.IAMPolicy.html)
- [AWS: Troubleshooting IAM database authentication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.Troubleshooting.html)
- [AWS: SSL/TLS for RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)
- [AWS: EKS identity and access management best practices](https://docs.aws.amazon.com/eks/latest/best-practices/identity-and-access-management.html)
- [AWS Advanced JDBC Wrapper](https://github.com/aws/aws-advanced-jdbc-wrapper)
- [AWS Advanced Python Wrapper](https://github.com/aws/aws-advanced-python-wrapper)
