vim.opt.runtimepath:prepend(vim.env.JUSI_NVIM_ROOT)
local jusi = require("jusi")
local python = vim.env.JUSI_TEST_PYTHON
local notices = {}
vim.notify = function(message) table.insert(notices, tostring(message)) end
local function wait_for(predicate, message)
  assert(vim.wait(15000, predicate, 20), message .. "\n" .. table.concat(notices, "\n"))
end
jusi.setup({terminal_bridge_command = {python, "-m", "jusi", "terminal-bridge"}})
local buf = vim.api.nvim_create_buf(false, true)
vim.api.nvim_win_set_buf(0, buf)
vim.api.nvim_buf_set_lines(buf, 0, -1, false, {"╭──", "%%acp fixture", "questions-many", "╰──"})
local service = jusi.start_service({buf=buf, command={python, "-m", "jusi", "serve"}, timeout_ms=8000})
local result = {}
local ok, failure = xpcall(function()
  wait_for(function()
    return jusi._sessions[buf] and jusi._sessions[buf].controller.transport_state == "connected"
  end, "service did not connect")
  local session = jusi._sessions[buf]
  jusi.start_kernel(buf)
  wait_for(function() return session.controller.kernel_state == "on" end, "kernel did not start")
  jusi.execute(buf, 1)
  wait_for(function() return next(session.interactive.surfaces) ~= nil end, "no terminal surface")
  local surface_id, surface = next(session.interactive.surfaces)
  local client_id = surface.client.client_id
  local function screen()
    return table.concat(vim.api.nvim_buf_get_lines(surface.buf, 0, -1, false), "\n")
  end
  wait_for(function() return screen():find("Question 1 of 2", 1, true) end, "first question not displayed")
  assert(vim.api.nvim_get_current_buf() == buf, "question stole notebook focus")
  local function edit_body(lines)
    -- All answer text is edited in the source cell. No terminal keys are sent.
    local cell = session.model:cell_by_id(surface.client.cell_id)
    local snapshot = session.model:cell_snapshot(cell)
    local first = snapshot.body_start_row
    vim.api.nvim_buf_set_lines(buf, first, snapshot.body_end_row, false, lines)
  end
  local function followup(lines)
    edit_body(lines)
    local response, err
    session.controller:followup(surface.client.cell_id, function(r, e) response, err = r, e end)
    wait_for(function() return response or err end, "follow-up remained blocked")
    assert(not err, vim.inspect(err))
    assert(response.operation.outcome == "succeeded", vim.inspect(response))
    assert(session.controller.clients[client_id], "answer replaced client")
    assert(session.interactive.surfaces[surface_id] == surface, "answer replaced terminal")
    assert(vim.api.nvim_get_current_buf() == buf, "answer stole notebook focus")
    return response
  end
  result.first = followup({"Application"})
  wait_for(function() return screen():find("Question 2 of 2", 1, true) end, "second question not displayed")
  result.second = followup({"  Preserve indentation.", "α and β", "/config is literal answer text."})
  wait_for(function() return screen():find("end_turn", 1, true) end, "turn did not finish")
  -- A question reached through a normal follow-up must also release the worker.
  result.next_turn = followup({"questions"})
  wait_for(function() return screen():find("Question 1 of 1", 1, true) end, "later turn did not ask")
  result.cancel = followup({"/cancel"})
  result.waiting_work = followup({"questions-wait"})
  edit_body({"Application"})
  local interrupted, interrupt_error
  session.controller:followup(surface.client.cell_id, function(r, e) interrupted, interrupt_error = r, e end)
  wait_for(function() return next(session.controller.client_operations) ~= nil end, "answer operation has no identity")
  jusi.interrupt(buf, 1)
  wait_for(function() return interrupted or interrupt_error end, "answer operation did not stop")
  assert(not interrupt_error, vim.inspect(interrupt_error))
  assert(interrupted.operation.outcome == "cancelled", vim.inspect(interrupted))
  result.reuse = followup({"healthy"})
  wait_for(function() return screen():find("end_turn", 1, true) end, "final turn did not redraw")
  result.screen = screen()
  result.history = {}
  local snapshot = session.model:cell_snapshot(session.model:cell_by_id(surface.client.cell_id))
  for _, entry in ipairs(snapshot.history_entries) do
    table.insert(result.history, table.concat(vim.api.nvim_buf_get_lines(buf, entry.start_row, entry.end_row, false), "\n"))
  end
end, debug.traceback)
if jusi._sessions[buf] then
  jusi.stop_service(buf)
  vim.wait(10000, function() return service.state == "stopped" end, 20)
end
result.ok, result.failure = ok, failure
result.stderr = service.stderr
vim.fn.writefile({vim.json.encode(result)}, vim.env.JUSI_TEST_RESULT)
vim.cmd("qa!")
