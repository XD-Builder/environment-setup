" {{{ Abbreviation
cnoreabbrev Q q
cnoreabbrev Q! q!
cnoreabbrev Qall qall
cnoreabbrev Qall! qall!
cnoreabbrev W w
cnoreabbrev W! w!
cnoreabbrev WQ wq
cnoreabbrev Wa wa
cnoreabbrev Wq wq
cnoreabbrev wQ wq
cnoreabbrev ag Rg
cnoreabbrev Ag Rg
"}}}
" {{{ Global Editor Mapping
" quick fix
nnoremap <silent><leader>q :QFix<cr>
nnoremap <leader>} :lnext<cr>
nnoremap <leader>{ :lprev<cr>

map <C-h> <C-w>h
map <C-j> <C-w>j
map <C-k> <C-w>k
map <C-l> <C-w>l
nnoremap <silent><leader>tc :tabclose<CR>
nnoremap <silent><leader>tn :tabnext<CR>
nnoremap <silent><leader>tp :tabprevious<CR>
nnoremap <silent><leader>tt :tabnew<CR>
nnoremap <silent><leader>wh :exe "vertical resize " . (winwidth(0) * 2/3)<CR>
nnoremap <silent><leader>wj :exe "resize " . (winheight(0) * 3/2)<CR>
nnoremap <silent><leader>wk :exe "resize " . (winheight(0) * 2/3)<CR>
nnoremap <silent><leader>wl :exe "vertical resize " . (winwidth(0) * 3/2)<CR>
noremap <silent><leader>bc :bd<CR>
noremap <silent><leader>bn :bn<CR>
noremap <silent><leader>bp :bp<CR>

nmap <silent><Leader>rv :e $MYVIMRC<CR>
nmap <silent><Leader>rb :e $HOME/.vim/custom/bindings.vim<CR>
nmap <silent><Leader>rs :e $HOME/.vim/custom/settings.vim<CR>
nmap <silent><Leader>rf :e $HOME/.vim/custom/functions.vim<CR>
nmap <silent><Leader>rp :e $HOME/.vim/custom/plugins.vim<CR>
nmap <silent><Leader>sv :so $MYVIMRC<CR>
nmap <silent><leader>/ :nohlsearch<CR>
nmap <silent><leader>f [s1z=<c-o>
nmap <silent><leader>sa ggVG
nmap <silent><leader>sb :buffers<cr>
nmap <silent><leader>sr :reg<cr>
nnoremap <leader>x :update<CR>
nnoremap <space> za
noremap <Leader>E :qa!<CR>
noremap <Leader>e :quit<CR>
vmap <S-Tab> <gv
vmap <Tab> >gv
vnoremap <silent><Leader>s :sort<CR>
inoremap <c-c> <esc>:write<cr>
nnoremap <c-c> :write<cr>
inoremap jk <esc>:update<cr>
" do not save replaced text in reg when pasting
xnoremap p pgvy

nnoremap <leader>Tp :set paste!<cr>
nnoremap <leader>Ts :set spell!<cr>

" Insert mode bash emulation
inoremap <C-f> <C-o>a
inoremap <C-b> <C-o>h
inoremap <C-e> <C-o>$
inoremap <C-a> <C-o>0
inoremap <C-j> <C-o>j
inoremap <C-k> <C-o>k
inoremap <C-u> <C-o>d0<C-o>dl
inoremap <C-w> <Space><C-o>B<c-o>dW
"}}}
" {{{ Global
nmap <silent><leader><space> :EssentialsRemoveWhiteSpace<CR>
nmap <leader>z :NERDTreeToggle<CR>
nmap <silent><leader>a :TagbarToggle<CR>
" }}}
" {{{ Common Binding
" Git integration
nnoremap <leader>cga :Git add % :p<CR><CR>
nnoremap <leader>cgA :Git add . <CR><CR>
nnoremap <leader>cgs :Gstatus<CR>
nnoremap <leader>cgc :Gcommit -v -q<CR>
nnoremap <leader>cgt :Gcommit -v -q %:p<CR>
nnoremap <leader>cgd :Gdiff HEAD<CR>
nnoremap <leader>cge :Gedit<CR>
nnoremap <leader>cgr :Gread<CR>
nnoremap <leader>cgw :Gwrite<CR><CR>
nnoremap <leader>cgl :silent! Glog<CR>:bot copen<CR>
nnoremap <leader>cgp :Ggrep<Space>
nnoremap <leader>cgm :Gmove<Space>
nnoremap <leader>cgb :Git branch<Space>
nnoremap <leader>cgco :Git checkout<Space>

" Easy motion
nmap <leader>cs <Plug>(easymotion-overwin-f2)
map <Leader>csj <Plug>(easymotion-j)
map <Leader>csk <Plug>(easymotion-k)

" fzf fuzzy finder (grouped under <leader>p to avoid clashing with existing prefixes)
nnoremap <silent><C-p> :Files<CR>
nnoremap <silent><leader>pf :Files<CR>
nnoremap <silent><leader>pb :Buffers<CR>
nnoremap <silent><leader>pr :Rg<CR>

"}}}
" {{{ Miscellaneous Binding
" Tabularize
if exists(":Tabularize")
    nmap <leader>mt= :Tabularize /=<CR>
    vmap <leader>mt= :Tabularize /=<CR>
    nmap <leader>mt: :Tabularize /:\zs<CR>
    vmap <leader>mt: :Tabularize /:\zs<CR>
    nmap <leader>mt" :Tabularize /"<CR>
    vmap <leader>mt" :Tabularize /"<CR>
endif

"vim test
nmap <silent> <leader>mt<C-n> :TestNearest<CR>
nmap <silent> <leader>mt<C-f> :TestFile<CR>
nmap <silent> <leader>mt<C-s> :TestSuite<CR>
nmap <silent> <leader>mt<C-l> :TestLast<CR>
nmap <silent> <leader>mt<C-g> :TestVisit<CR>

" }}}
" {{{ Utility Binding
nnoremap <leader>ut "=strftime("%c")<CR>P
inoremap <leader>ut <C-R>=strftime("%c")<CR>
nnoremap <leader>ud :exe ":!clear;sdcv ". expand("<cword>")<cr>
map <leader>uW :w !sudo tee % >/dev/null<CR>

" Google search, define and goto url
nnoremap <leader>ugo :Google<cr>
nnoremap <leader>ugd :exe ":Google define ". expand("<cword>")<cr>
nnoremap <leader>ugu :EssentialsOpenUrl<cr>
" }}}
" {{{ Function Keys
" F1 - Help on word under cursor. Yank inner word into register z and run help
" on it on that register both in normal and insert mode
map <F1> "zyiw:exe "h ".@z.""<CR>

" F2 - Dispatch commands to run against the current file and show result in a
" quickfix window
nnoremap <F2> :Dispatch<CR>

function! SelectVisualModeText()
    " Why is this not a built-in Vim script function?!
    let [line_start, column_start] = getpos("'<")[1:2]
    let [line_end, column_end] = getpos("'>")[1:2]
    let lines = getline(line_start, line_end)
    if len(lines) == 0
        return ''
    endif
    let lines[-1] = lines[-1][: column_end - (&selection == 'inclusive' ? 1 : 2)]
    let lines[0] = lines[0][column_start - 1:]
    return join(lines, "\n")
endfunction
" F3 - Selects the text and runs stackoverflow query and put the result in the
" quickfix window. Type ,q for quickfix window
vnoremap <F3> :EssentialsStackOverflow(SelectVisualModeText())<CR>

" Redraw screen
nnoremap <silent><F5> :redraw!<CR>
" }}}
" {{{ Commands and augroups
" Run Command Background Command
command! -nargs=+ -complete=shellcmd RunBackgroundCommand call RunBackgroundCommand(<q-args>)

" Quick Fix Command
command! -bang -nargs=? QFix call QFixToggle(<bang>0)

" Chanage Tabs Command
command! -nargs=* Tab call Tab()


augroup filetype_action
    autocmd!
    autocmd   filetype   python                    setlocal   foldmethod=indent
    autocmd   filetype   python                    setlocal   foldlevel=99
    autocmd   FileType   python                    set        softtabstop=4       tabstop=4   shiftwidth=4
    autocmd   FileType   ruby,rdoc,cucumber,yaml   set        softtabstop=2       tabstop=2   shiftwidth=2
augroup END

augroup filetype_set
    autocmd!
    autocmd   BufNewFile,BufRead   *.jade        setlocal      ft=jade
    autocmd   BufNewFile,BufRead   *.less        setlocal      ft=less
    autocmd   BufNewFile,BufRead   *.md          setlocal      ft=markdown
    autocmd   BufNewFile,BufRead   *.twig        setlocal      ft=html
    autocmd   BufNewFile,BufRead   Gemfile       setfiletype   ruby
    autocmd   BufNewFile,BufRead   Isolate       setfiletype   ruby
    autocmd   BufNewFile,BufRead   Vagrantfile   setfiletype   ruby
    autocmd   BufNewFile,BufRead   config.ru     setfiletype   ruby
augroup END

augroup QFixToggle
    autocmd!
    autocmd   BufWinEnter   quickfix   let   g:qfix_win             =    bufnr("$")
    autocmd   BufWinLeave   *          if    exists("g:qfix_win")   &&   expand("<abuf>")   ==   g:qfix_win   |   unlet!   g:qfix_win   |   endif
augroup END
