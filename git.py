import os
import sublime
import sublime_plugin
import threading
import subprocess
import functools

def main_thread(callback, *args, **kwargs):
    # sublime.set_timeout gets used to send things onto the main thread
    # most sublime.[something] calls need to be on the main thread
    sublime.set_timeout(functools.partial(callback, *args, **kwargs), 0)

def open_url(url):
    sublime.active_window().run_command('open_url', {"url": url})

def git_root(directory):
    while directory:
        if os.path.exists(os.path.join(directory, '.git')):
            return directory
        parent = os.path.realpath(os.path.join(directory, os.path.pardir))
        if parent == directory:
            # /.. == /
            return False
        directory = parent
    return False

def _make_text_safeish(text):
    # The unicode decode here is because sublime converts to unicode inside insert in such a way
    # that unknown characters will cause errors, which is distinctly non-ideal...
    # and there's no way to tell what's coming out of git in output. So...
    return text.decode('utf-8')

class CommandThread(threading.Thread):
    def __init__(self, command, on_done, working_dir = "", ):
        threading.Thread.__init__(self)
        self.command = command
        self.on_done = on_done
        self.working_dir = working_dir

    def run(self):
        try:
            # Per http://bugs.python.org/issue8557 shell=True is required to get
            # $PATH on Windows. Yay portable code.
            if self.working_dir != "":
                os.chdir(self.working_dir)
            output = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True).communicate()[0]
            # if sublime's python gets bumped to 2.7 we can just do:
            # output = subprocess.check_output(self.command)
            main_thread(self.on_done, _make_text_safeish(output))
        except subprocess.CalledProcessError, e:
            main_thread(self.on_done, e.returncode)

class GitCommand(sublime_plugin.TextCommand):
    def run_command(self, command, callback = None, show_status = True, filter_empty_args = True, **kwargs):
        if filter_empty_args:
            command = [arg for arg in command if arg]
        if 'working_dir' not in kwargs:
            kwargs['working_dir'] = self.get_file_location()
        
        thread = CommandThread(command, callback or self.generic_done, **kwargs)
        thread.start()

        if show_status:
            message = kwargs.get('status_message', False) or ' '.join(command)
            sublime.status_message(message)

    def generic_done(self, result):
        if not result.strip():
            return
        self.panel(result)

    def _output_to_view(self, output_file, output, clear = False, syntax = "Packages/Diff/Diff.tmLanguage"):
        output_file.set_syntax_file(syntax)
        edit = output_file.begin_edit()
        if clear:
            region = sublime.Region(0, self.output_view.size())
            output_file.erase(edit, region)
        output_file.insert(edit, 0, output)
        output_file.end_edit(edit)

    def scratch(self, output, title = False, **kwargs):
        scratch_file = self.view.window().new_file()
        if title:
            scratch_file.set_name(title)
        scratch_file.set_scratch(True)
        self._output_to_view(scratch_file, output, **kwargs)
        scratch_file.set_read_only(True)
        return scratch_file
    
    def panel(self, output, **kwargs):
        if not hasattr(self, 'output_view'):
            self.output_view = self.view.window().get_output_panel("git")
        self.output_view.set_read_only(False)
        self._output_to_view(self.output_view, output, clear = True, **kwargs)
        self.output_view.set_read_only(True)
        self.view.window().run_command("show_panel", {"panel": "output.git"})

    def is_enabled(self):
        # First, is this actually a file on the file system?
        if self.view.file_name() and len(self.view.file_name()) > 0:
            return git_root(self.get_file_location())
    def get_file_name(self):
        return os.path.basename(self.view.file_name())
    def get_file_location(self):
        return os.path.dirname(self.view.file_name())

class GitBlameCommand(GitCommand):
    def run(self, edit):
        # somewhat custom blame command:
        # -w: ignore whitespace changes
        # -M: retain blame when moving lines
        # -C: retain blame when copying lines between files
        command = ['git', 'blame', '-w', '-M', '-C']

        selection = self.view.sel()[0] # todo: multi-select support?
        if not selection.empty():
            # just the lines we have a selection on
            begin_line, begin_column = self.view.rowcol(selection.begin())
            end_line, end_column = self.view.rowcol(selection.end())
            lines = str(begin_line) + ',' + str(end_line)
            command.extend(('-L', lines))

        command.append(self.get_file_name())
        self.run_command(command, self.blame_done)
    def blame_done(self, result):
        self.scratch(result, title = "Git Blame")

class GitLogCommand(GitCommand):
    def run(self, edit):
        # the ASCII bell (\a) is just a convenient character I'm pretty sure won't ever come
        # up in the subject of the commit (and if it does then you positively deserve broken
        # output...)
        # 9000 is a pretty arbitrarily chosen limit; picked entirely because it's about the size
        # of the largest repo I've tested this on... and there's a definitely hiccup when it's
        # loading
        self.run_command(['git', 'log', '--pretty=%s\a%h %an <%aE>\a%ad (%ar)', '--date=local', '--max-count=9000', '--', self.get_file_name()], self.log_done)
    
    def log_done(self, result):
        self.results = [r.split('\a', 2) for r in result.strip().split('\n')]
        self.view.window().show_quick_panel(self.results, self.panel_done)
    
    def panel_done(self, picked):
        if picked == -1:
            return
        if 0 > picked > len(self.results):
            return
        item = self.results[picked]
        # the commit hash is the first thing on the second line
        ref = item[1].split(' ')[0]
        # I'm not certain I should have the file name here; it restricts the details to just
        # the current file. Depends on what the user expects... which I'm not sure of.
        self.run_command(['git', 'log', '-p', '-1', ref, '--', self.get_file_name()], self.details_done)
    
    def details_done(self, result):
        self.scratch(result, title = "Git Commit Details")

class GitLogAllCommand(GitLogCommand):
    def get_file_name(self):
        return ''

class GitDiffCommand(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'diff', self.get_file_name()], self.diff_done)
    
    def diff_done(self, result):
        self.scratch(result, title = "Git Diff")

class GitDiffAllCommand(GitDiffCommand):
    def get_file_name(self):
        return ''

class GitCommitCommand(GitCommand):
    def run(self, edit):
        self.view.window().show_input_panel("Message", "", self.on_input, None, None)
    
    def on_input(self, message):
        if message.strip() == "":
            # Okay, technically an empty commit message is allowed, but I don't want to encourage that sort of thing
            sublime.error_message("No commit message provided")
            return
        self.run_command(['git', 'add', self.get_file_name()], functools.partial(self.add_done, message))
    
    def add_done(self, message, result):
        if result.strip():
            sublime.error_message("Error adding file:\n" + result)
            return
        self.run_command(['git', 'commit', '-m', message])

class GitStatusCommand(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'status', '--porcelain'], self.status_done)
    def status_done(self, result):
        self.results = filter(self.status_filter, result.rstrip().split('\n'))
        self.view.window().show_quick_panel(self.results, self.panel_done, sublime.MONOSPACE_FONT)
    def status_filter(self, item):
        # for this class we don't actually care
        return True
    def panel_done(self, picked):
        if picked == -1:
            return
        if 0 > picked > len(self.results):
            return
        picked_file = self.results[picked]
        # first 3 characters are status codes
        picked_file = picked_file[3:]
        self.panel_followup(picked_file)
    def panel_followup(self, picked_file):
        # split out solely so I can override it for laughs
        self.run_command(['git', 'diff', picked_file], self.diff_done, working_dir = git_root(self.get_file_location()))
    
    def diff_done(self, result):
        self.scratch(result, title = "Git Diff")

class GitAddChoiceCommand(GitStatusCommand):
    def status_filter(self, item):
        return not item[1].isspace()
    def panel_followup(self, picked_file):
        self.run_command(['git', 'add', picked_file], working_dir = git_root(self.get_file_location()))

class GitAdd(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'add', self.get_file_name()])

class GitStashCommand(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'stash'])

class GitStashPopCommand(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'stash', 'pop'])

class GitBranchCommand(GitCommand):
    def run(self, edit):
        self.run_command(['git', 'branch'], self.branch_done)
    def branch_done(self, result):
        self.results = result.rstrip().split('\n')
        self.view.window().show_quick_panel(self.results, self.panel_done, sublime.MONOSPACE_FONT)
    def panel_done(self, picked):
        if picked == -1:
            return
        if 0 > picked > len(self.results):
            return
        picked_branch = self.results[picked]
        if picked_branch.startswith("*"):
            return
        picked_branch = picked_branch.strip()
        self.run_command(['git', 'checkout', picked_branch])
