vim.opt.runtimepath:prepend(vim.env.JUSI_NVIM_ROOT)
local jusi = require('jusi')
local python = vim.env.JUSI_TEST_PYTHON
local notices = {}
vim.notify = function(message) notices[#notices + 1] = tostring(message) end
local function wait_for(predicate, message)
  assert(vim.wait(15000, predicate, 20), message .. '\n' .. table.concat(notices, '\n'))
end
jusi.setup({ terminal_bridge_command = { python, '-m', 'jusi', 'terminal-bridge' } })
local buf = vim.api.nvim_create_buf(false, true)
vim.api.nvim_set_current_buf(buf)
local draft = { '╭──', '%%acp fixture', 'Do not send this draft', '╰──' }
vim.api.nvim_buf_set_lines(buf, 0, -1, false, draft)
local service = jusi.start_service({ buf=buf, command={python, '-m', 'jusi', 'serve'}, timeout_ms=8000 })
local result = {}
local ok, failure = xpcall(function()
  wait_for(function() return jusi._sessions[buf] and jusi._sessions[buf].controller.transport_state == 'connected' end, 'service connection')
  local session = jusi._sessions[buf]
  jusi.start_kernel(buf)
  wait_for(function() return session.controller.kernel_state == 'on' end, 'kernel startup')
  local response, err
  jusi.bootstrap(buf, 1, function(r,e) response,err = r,e end)
  wait_for(function() return response or err end, 'bootstrap response')
  assert(not err and response.execution.outcome == 'succeeded', vim.inspect(err or response))
  local id, cell = response.execution.client_id, session.model:cell_at_row(1)
  wait_for(function() return session.controller.clients[id] and next(session.interactive.surfaces) end, 'client surface')
  response, err = nil, nil
  session.controller:complete({ kernel_id=session.controller.kernel_id, notebook_id=session.model.notebook_id,
    cell_id=cell.id, client_id=id, body='@', cursor_pos=1 }, function(r,e) response,err = r,e end)
  wait_for(function() return response or err end, 'pre-turn completion')
  assert(not err, vim.inspect(err))
  local found = false
  for _, item in ipairs(response.completion.items) do if item.text == '@sample.py' then found = true end end
  assert(found, vim.inspect(response.completion))
  assert(vim.deep_equal(draft, vim.api.nvim_buf_get_lines(buf, 0, -1, false)))
  assert(#session.model:history(cell.id) == 0)
  response, err = nil, nil
  jusi.bootstrap(buf, 1, function(r,e) response,err = r,e end)
  wait_for(function() return response or err end, 'repeat bootstrap')
  assert(not err and response.client.client_id == id, vim.inspect(err))
  result.bootstrap = true
end, debug.traceback)
if jusi._sessions[buf] then
  jusi.stop_service(buf)
  vim.wait(10000, function() return service.state == 'stopped' end, 20)
end
result.ok, result.failure, result.stderr = ok, failure, service.stderr
vim.fn.writefile({vim.json.encode(result)}, vim.env.JUSI_TEST_RESULT)
vim.cmd('qa!')
