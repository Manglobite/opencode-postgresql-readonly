import { tool } from "@opencode-ai/plugin"
import path from "path"

import { getDatabaseConfig, listDatabaseProjects } from "./config.js"

const scriptPath = path.join(import.meta.dirname, "db", "exec.py")

type PythonToolInput = {
  query: string
  config: {
    driver: string
    host: string
    port: number
    database: string
    user: string
    password: string
    sslmode: string
  }
}

function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  return String(error)
}

async function runPythonTool(input: PythonToolInput): Promise<string> {
  const proc = Bun.spawn(["python3", scriptPath], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  })

  proc.stdin.write(JSON.stringify(input))
  proc.stdin.end()

  const output = await new Response(proc.stdout).text()
  const stderr = await new Response(proc.stderr).text()
  await proc.exited

  if (proc.exitCode === 0) {
    return output.trimEnd()
  }

  throw new Error(stderr.trim() || output.trim() || `db/exec.py exited with code ${proc.exitCode}`)
}

export const database_exec = tool({
  description: "Execute a read-only Postgres SELECT query",
  args: {
    project: tool.schema.string().describe("Database project name from config.json"),
    environment: tool.schema.string().optional().describe("Database environment name"),
    query: tool.schema.string().describe("A single SQL SELECT query to execute"),
  },
  async execute(args) {
    if (!args.project?.trim()) {
      return "Не указан project"
    }

    try {
      const config = await getDatabaseConfig(args.project, args.environment)

      return await runPythonTool({
        query: args.query,
        config: {
          driver: config.driver,
          host: config.host,
          port: config.port,
          database: config.database,
          user: config.user,
          password: config.password,
          sslmode: config.sslmode,
        },
      })
    } catch (error) {
      return formatError(error)
    }
  },
})

export const database_list = tool({
  description: "List available database projects and their environments",
  args: {},
  async execute() {
    try {
      const projects = await listDatabaseProjects()
      return JSON.stringify(projects, null, 2)
    } catch (error) {
      return formatError(error)
    }
  },
})
