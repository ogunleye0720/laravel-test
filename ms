Passwordless authentication for Aurora PostgreSQL

We’re introducing IAM-based authentication for applications connecting to Aurora PostgreSQL from EKS, Lambda, ECS, and EC2.

Instead of storing and rotating a database password, applications will use their AWS workload identity to generate a short-lived authentication token. This improves security by removing long-lived database credentials from application configuration.

What changes for application teams

Request rds-db:connect access for your workload’s IAM role.

Connect using the platform-provisioned IAM database user.

Generate an IAM authentication token when opening a new database connection, or use an AWS Advanced Wrapper that handles token refresh.

Continue using the same database endpoint, port, database, ORM, and queries.

Existing applications will not be changed automatically and can continue using their current credentials until they are ready to migrate. Teams should test IAM authentication and connection-pool behaviour before removing existing credentials.

Implementation examples, migration steps, and troubleshooting guidance are available here: [Passwordless Aurora PostgreSQL guide — add Wiki link]
