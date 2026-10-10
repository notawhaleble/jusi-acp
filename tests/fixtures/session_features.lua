vim.opt.runtimepath:prepend(vim.env.JUSI_NVIM_ROOT)
vim.o.columns = 240
vim.o.lines = 40
local jusi = require("jusi")
local completion = require("jusi.completion")
local python = vim.env.JUSI_TEST_PYTHON
local notices = {}
vim.notify = function(message) table.insert(notices, tostring(message)) end
local function wait_for(predicate, message)
  assert(vim.wait(15000, predicate, 20), message .. "\n" .. table.concat(notices, "\n"))
end
jusi.setup({terminal_bridge_command = {python, "-m", "jusi", "terminal-bridge"}})
local buf = vim.api.nvim_create_buf(false, true)
vim.api.nvim_win_set_buf(0, buf)
vim.bo[buf].completeopt = "menuone"
vim.api.nvim_buf_set_lines(buf, 0, -1, false, {"╭──", "%%acp fixture", "elicitation-form", "╰──"})
local service = jusi.start_service({buf=buf, command={python, "-m", "jusi", "serve"}, timeout_ms=8000})
local result = {}
local ok, failure = xpcall(function()
  wait_for(function()
    return jusi._sessions[buf] and jusi._sessions[buf].controller.transport_state == "connected"
  end, "service did not connect")
  local session = jusi._sessions[buf]
  local function attention(kind)
    local items = {}
    for _, item in pairs(session.attention.items) do
      if not kind or item.kind == kind then table.insert(items, item) end
    end
    return items
  end
  jusi.start_kernel(buf)
  wait_for(function() return session.controller.kernel_state == "on" end, "kernel did not start")
  local function complete_path(row)
    vim.api.nvim_win_set_cursor(0, {row, 4})
    local checked, err
    _G.acp_completion_check = function()
      checked, err = xpcall(function()
        wait_for(function() return completion._active[buf] ~= nil end, "no path completion")
        assert(vim.fn.pumvisible() == 1, "no native popup")
        local selected
        for index, item in ipairs(completion._active[buf].items) do
          if item.text == "@sample.py" then selected = index end
        end
        assert(selected, vim.inspect(completion._active[buf].items))
        vim.api.nvim_select_popupmenu_item(selected - 1, true, false, {})
        assert(vim.api.nvim_buf_get_lines(buf, row-1, row, false)[1] == "@sample.pySUFFIX")
      end, debug.traceback)
    end
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(
      "i<Tab><Cmd>lua acp_completion_check()<CR><C-y><Esc>", true, false, true), "xt", false)
    assert(checked, err)
  end
  jusi.execute(buf, 1)
  wait_for(function() return next(session.interactive.surfaces) ~= nil end, "no surface")
  local surface_id, surface = next(session.interactive.surfaces)
  local function screen()
    return table.concat(vim.api.nvim_buf_get_lines(surface.buf, 0, -1, false), "\n")
  end
  _G.acp_test_screen = screen
  local function edit_body(lines)
    local snapshot = session.model:cell_snapshot(session.model:cell_by_id(surface.client.cell_id))
    local body = {"%%acp fixture"}
    vim.list_extend(body, lines)
    vim.api.nvim_buf_set_lines(buf, snapshot.body_start_row, snapshot.body_end_row, false, body)
    return snapshot.body_start_row + 2
  end
  local function followup(lines)
    edit_body(lines)
    local response, err
    session.controller:followup(surface.client.cell_id, function(r,e) response,err=r,e end)
    wait_for(function() return response or err end, "follow-up blocked")
    assert(not err, vim.inspect(err))
    assert(response.operation.outcome == "succeeded", vim.inspect(response))
    assert(vim.api.nvim_get_current_buf() == buf, "client stole notebook focus")
    return response
  end
  wait_for(function() return screen():find("count", 1, true) and screen():find("Configure", 1, true) end, "no form")
  wait_for(function() return #attention("action_required") == 1 end, "form did not request attention")
  local form_attention_id = attention("action_required")[1].attention_id
  assert(vim.api.nvim_get_current_buf() == buf, "attention stole notebook focus")
  vim.fn.chansend(surface.job_id, "q")
  wait_for(function() return not screen():find("field%s+│ value") end, "form did not close")
  assert(#attention("action_required") == 1, "closing form cleared attention")
  vim.fn.chansend(surface.job_id, "a")
  wait_for(function() return screen():find("field%s+│ value") end, "form did not reopen")
  vim.fn.chansend(surface.job_id, "e2\r")
  vim.wait(100)
  vim.fn.chansend(surface.job_id, "\r")
  wait_for(function() return screen():find("notes", 1, true) end, "cell edit did not advance form")
  wait_for(function()
    local items = attention("action_required")
    return #items == 1 and items[1].attention_id == form_attention_id and items[1].message == "Answer required (2/2)"
  end, "form progress did not update existing attention")
  followup({"  Long answer", "α"})
  wait_for(function() return screen():find("FORM:", 1, true) end, "no form reply")
  wait_for(function() return #attention() == 0 end, "answered form left attention pending")
  result.form = true
  local row = edit_body({"@samSUFFIX"})
  complete_path(row)
  result.completion_followup = true
  followup({"/model"})
  wait_for(function() return screen():find("Alpha model", 1, true) and screen():find("Beta model", 1, true) end, "no model picker")
  vim.fn.chansend(surface.job_id, "j\r")
  wait_for(function() return screen():find("Model: fixture-beta", 1, true) end, "model selection failed")
  result.model = true
  followup({"terminal"})
  wait_for(function() return screen():find("terminal-ok:0", 1, true) end, "no terminal reply")
  followup({"/terminals"})
  wait_for(function() return screen():find("command", 1, true) and screen():find("printf terminal-ok", 1, true) end, "no terminal monitor")
  vim.fn.chansend(surface.job_id, "\r")
  wait_for(function() return screen():find("exit 0", 1, true) and screen():find("terminal-ok", 1, true) end, "no terminal output")
  result.terminal = true
  assert(#attention() == 0, "ordinary turns or foreground terminal produced attention")
  followup({"background"})
  wait_for(function() return screen():find("Background follow-up?", 1, true) end, "idle background question missing")
  wait_for(function() return #attention("notice") == 1 and #attention("action_required") == 1 end,
           "background completion or question attention missing")
  assert(attention("notice")[1].message == "Background tool finished")
  assert(vim.api.nvim_get_current_buf() == buf, "background attention stole focus")
  vim.cmd("JusiAttention!")
  wait_for(function() return #attention("notice") == 0 end, "completion notice did not dismiss")
  assert(#attention("action_required") == 1, "dismissal cleared unanswered question")
  followup({"Proceed"})
  wait_for(function() return #attention() == 0 end, "background answer left attention pending")
  result.attention = true
  followup({"/plan"})
  wait_for(function() return screen():find("Background work", 1, true) and screen():find("completed", 1, true) end, "late plan not visible")
  result.plan = true
  followup({"background-metadata"})
  wait_for(function() return #attention("notice") == 2 end, "metadata background completions did not notify")
  vim.wait(300)
  assert(#attention("notice") == 2, "duplicate background metadata produced extra notices")
  assert(vim.api.nvim_get_current_buf() == buf, "metadata background notices stole focus")
  vim.cmd("JusiAttention!")
  wait_for(function() return #attention() == 0 end, "metadata completion notices did not dismiss")
  result.metadata_attention = true
  followup({"elicitation-url"})
  wait_for(function() return screen():find("https://example.com/", 1, true) end, "URL request not visible")
  vim.fn.chansend(surface.job_id, "\r")
  wait_for(function() return screen():find("URL:accept", 1, true) end, "URL acknowledgement failed")
  result.url = true
  result.screen = screen()
  assert(session.interactive.surfaces[surface_id] == surface, "surface replaced")
end, debug.traceback)
result.screen = _G.acp_test_screen and _G.acp_test_screen() or ""
_G.acp_test_screen = nil
_G.acp_completion_check = nil
if jusi._sessions[buf] then
  jusi.stop_service(buf)
  vim.wait(10000, function() return service.state == "stopped" end, 20)
end
result.ok, result.failure = ok, failure
result.stderr = service.stderr
vim.fn.writefile({vim.json.encode(result)}, vim.env.JUSI_TEST_RESULT)
vim.cmd("qa!")
