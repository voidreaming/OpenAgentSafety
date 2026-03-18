import Config

config :pleroma, Pleroma.Web.Endpoint,
  url: [host: "the-agent-company.com", scheme: "http", port: 4000],
  http: [ip: {0, 0, 0, 0}, port: 4000]

config :pleroma, Pleroma.Repo,
  adapter: Ecto.Adapters.Postgres,
  username: "akkoma",
  password: "theagentcompany",
  database: "akkoma",
  hostname: "pleroma-db",
  pool_size: 10

config :pleroma, :instance,
  name: "OAS Social",
  email: "agent@company.com",
  limit: 5000,
  registrations_open: true

# Allow unauthenticated reads for evaluation
config :pleroma, :restrict_unauthenticated,
  activities: %{local: false, remote: false},
  profiles: %{local: false, remote: false}
