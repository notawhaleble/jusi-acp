vim.opt.runtimepath:prepend(vim.env.JUSI_NVIM_ROOT)
local capture
package.preload['jusi_neovim.attachments'] = function()
  return { register = function(_, handler) capture = handler; return function() end end }
end
local jusi = require('jusi')
local python = vim.env.JUSI_TEST_PYTHON
local notices = {}
vim.notify = function(message) notices[#notices + 1] = tostring(message) end
local function wait_for(predicate, message)
  assert(vim.wait(15000, predicate, 20), message .. '\n' .. table.concat(notices, '\n'))
end
jusi.setup({terminal_bridge_command = {python, '-m', 'jusi', 'terminal-bridge'}})
local buf = vim.api.nvim_get_current_buf()
vim.api.nvim_buf_set_lines(buf, 0, -1, false, {'╭──', '%%acp fixture', '╰──'})
local service = jusi.start_service({buf=buf, command={python, '-m', 'jusi', 'serve'}, timeout_ms=8000})
local ok, failure = xpcall(function()
  wait_for(function() return jusi._sessions[buf] and jusi._sessions[buf].controller.transport_state == 'connected' end, 'connect')
  local session = jusi._sessions[buf]
  jusi.start_kernel(buf)
  wait_for(function() return session.controller.kernel_state == 'on' end, 'kernel')
  local bytes = string.char(137,80,78,71,13,10,26,10,0,255)
  capture(1)({{name='test.png', media_type='image/png', bytes=bytes}})
  local cell = session.model:cell_at_row(1)
  local function submit(method)
    local response, failure
    method(buf, 1, function(r,e) response,failure = r,e end)
    wait_for(function() return response or failure end, 'submission')
    assert(not failure, vim.inspect(failure))
  end
  submit(jusi.execute)
  wait_for(function() return next(session.interactive.surfaces) end, 'client')
  -- The initial prompt is asynchronous. Wait for its agent reply before sending another.
  wait_for(function()
    return #vim.fn.glob(vim.env.JUSI_STATE_HOME .. '/**/events.jsonl', false, true) > 0 and
      table.concat(vim.fn.readfile(vim.fn.glob(vim.env.JUSI_STATE_HOME .. '/**/events.jsonl', false, true)[1]), '\n'):find('echo:image/png:',1,true)
  end, 'image-only response')
  assert(not session.editing.attachments[cell.id])
  local unsupported = pcall(capture(1), {{name='a.csv',media_type='text/csv',bytes='a,b'}})
  assert(not unsupported and not session.editing.attachments[cell.id])
  vim.api.nvim_buf_set_lines(buf, 2, 2, false, {'text-only followup'})
  submit(jusi.followup)
  vim.api.nvim_buf_set_lines(buf, 2, 3, false, {})
  capture(1)({{name='test.png',media_type='image/png',bytes=bytes}})
  submit(jusi.followup)
  assert(not session.editing.attachments[cell.id])
  capture(1)({{name='test.png',media_type='image/png',bytes=bytes}})
  vim.api.nvim_buf_set_lines(buf,0,-1,false,{'replacement'})
  wait_for(function() return session.editing.attachments[cell.id] == nil end, 'retired image linkage')
  assert(table.concat(notices,'\n'):find('lost test.png',1,true))
end, debug.traceback)
jusi.stop_service(buf)
vim.wait(10000, function() return service.state == 'stopped' end, 20)
vim.fn.writefile({vim.json.encode({ok=ok,failure=failure,stderr=service.stderr})}, vim.env.JUSI_TEST_RESULT)
vim.cmd('qa!')
