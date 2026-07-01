# Copyright © 2024 UNISI Tech. All rights reserved.
from .autotest import config

if not config.hot_reload:
    active_reloader = False
else:
    active_reloader = True
    import os, sys, traceback
    from watchdog.observers import Observer
    from watchdog.events import PatternMatchingEventHandler
    from .users import User, Redesign, empty_app
    from .utils import blocks_dir, divpath, app_dir, screens_dir
    from .autotest import check_module
    import re, collections

    #for removing message duplicates        
    file_content = collections.defaultdict(str)
    
    busy = False      
    cwd = os.getcwd()  
    request_file = None
    request_dependency_changed = False

    def free():
        global busy
        if request_file:
            reload(request_file, request_dependency_changed)
        else:
            busy = False

    def reload(sname, changed_dependency = False):
        user = User.last_user
        if user:
            file = open(f'{screens_dir}{divpath}{sname}', "r")
            content = file.read()
            if not changed_dependency and file_content[sname] == content:
                return
            file_content[sname] = content
            
            global busy, request_file, request_dependency_changed
            busy = True
            request_file = None
            request_dependency_changed = False

            try:
                module = user.load_screen(sname)
                errors = check_module(module)                                
                if errors:
                    print('\n'.join(errors))
                    busy = False
                    return 
                print('Reloaded.') 
            except:
                traceback.print_exc()        
                busy = False                
                return

            for i, s in enumerate(user.screens):
                if s.__file__ == module.__file__:
                    same = user.screen_module.__file__ == module.__file__
                    user.screens[i] = module
                    if same:
                        user.set_screen(module.screen.name)            
                    break
            else:
                user.screens.append(module)
                if len(user.screens) == 1:
                    user.set_screen(module.name)                    

            user.screens.sort(key=lambda s: s.screen.order)           
            user.update_menu()
            user.set_clean() 
            if hasattr(user,'send'):
                user.sync_send(Redesign)
            free()  
            return module  

    class ScreenEventHandler(PatternMatchingEventHandler):    
        def on_modified(self, event):
            if event.src_path.endswith('.py') and not event.is_directory and (user := User.last_user):                            
                short_path = event.src_path[len(cwd) + 1:]
                arr = short_path.split(divpath) 
                name = arr[-1]
                dir = arr[0] if len(arr) > 1 else ''                                                                                 
                changed_dependency = False

                if user.screen_module and dir not in [screens_dir, blocks_dir]:
                    changed_dependency = True
                    #analyze if dependency exist
                    file = open(user.screen_module.__file__, "r") 
                    arr[-1] = arr[-1][:-3]
                    module_name = '.'.join(arr) 
                    module_pattern = '\.'.join(arr)                        
                    
                    if re.search(f"((import|from)[ \t]*{module_pattern}[ \t\n]*)",file.read()):
                        if module_name in sys.modules:
                            del sys.modules[module_name]                            
                        short_path = user.screen_module.__file__
                        if short_path.startswith(app_dir):
                            short_path = short_path[len(app_dir) + 1:]
                        dir, name = short_path.split(divpath)                            

                if dir in [screens_dir, blocks_dir]:
                    if dir == blocks_dir:
                        user._drop_private_module(f'{blocks_dir}.{name[:-3]}')
                        #a block is a dependency of the current screen: force reload
                        #even though the screen file itself did not change
                        changed_dependency = True

                    if busy:
                        global request_file, request_dependency_changed
                        if dir == blocks_dir:
                            request_file = user.screen_module.__file__.split(divpath)[-1] if user.screen_module else None
                        else:
                            request_file = name
                        request_dependency_changed = changed_dependency
                    else:                    
                        fresh_module = reload(name, changed_dependency) if dir == screens_dir else None
                        module = user.screen_module
                        if module:
                            current = module.__file__
                            if not fresh_module or current != fresh_module.__file__:
                                reload(current.split(divpath)[-1], changed_dependency) 
                                                    
        def on_deleted(self, event):            
            if not event.is_directory and (user := User.last_user):                
                arr = event.src_path.split(divpath) 
                name = arr[-1]
                dir = arr[-2]  
                
                if name.endswith('.py') and dir == screens_dir:
                    user._remove_screen_info(name)
                    for i, s in enumerate(user.screens):
                        if s.__file__ == event.src_path:
                            user.screens.remove(s)
                            if user.screen_module is s:
                                if user.screens:                                                                        
                                    fname = user.screens[0].__file__.split(divpath)[-1]
                                    module = reload(fname) or user.screens[0]
                                    user.set_screen(module.name)
                                    user.update_menu()   
                                    if hasattr(user,'send'):                             
                                        user.sync_send(Redesign)      
                                elif user.screen_registry:
                                    info = user.screen_registry[0]
                                    module = user.ensure_screen(info.name or info.file)
                                    if module:
                                        user.set_screen(module.name)
                                        user.update_menu()
                                        if hasattr(user,'send'):
                                            user.sync_send(Redesign)
                                else:   
                                    if hasattr(user,'send'):                                                   
                                        user.sync_send(empty_app)                                                        
                            else:
                                reload(user.screen_module.__file__.split(divpath)[-1])
                                user.update_menu()
                                if hasattr(user,'send'):
                                    user.sync_send(Redesign)                                                        
                            break
                    user.update_menu()
                elif name.endswith('.py') and dir == blocks_dir:
                    user._drop_private_module(f'{blocks_dir}.{name[:-3]}')
                    if user.screen_module:
                        reload(user.screen_module.__file__.split(divpath)[-1], True)

    event_handler = ScreenEventHandler()
    observer = Observer()
    path = os.getcwd()
    observer.schedule(event_handler, path, recursive = True)
    observer.start()