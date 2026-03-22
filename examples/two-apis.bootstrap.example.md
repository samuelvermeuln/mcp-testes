# Two APIs Bootstrap Example

## API 1

Tool: `bootstrap_with_context`

```json
{
  "config_toml_path": "C:\\Users\\samuelv\\RiderProjects\\gosystem-test-mcp\\examples\\mobility-api.context.toml",
  "overwrite_agents": false,
  "project_root": null
}
```

## API 2

Tool: `bootstrap_with_context`

```json
{
  "config_toml_path": "C:\\Users\\samuelv\\RiderProjects\\gosystem-test-mcp\\examples\\mobility-app-api.context.toml",
  "overwrite_agents": false,
  "project_root": null
}
```

## Pipeline (cada API)

```json
{
  "config_toml_path": "C:\\Users\\samuelv\\RiderProjects\\gosystem-test-mcp\\examples\\mobility-api.context.toml",
  "project_root": null,
  "base_ref": "HEAD~1",
  "min_line_rate": 1.0
}
```
