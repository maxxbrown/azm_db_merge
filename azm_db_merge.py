infostr = '''azm_db_merge version 1.0 Copyright (c) 2016 Freewill FX Co., Ltd. All rights reserved.'''
usagestr = '''
Merge (import) AZENQOS Android .azm
test log files that contain SQLite3 database files (azqdata.db) into a target
central database (Now MS-SQL and SQLite3 only, later/roadmap: PostgreSQL and MySQL).\n

Please read SETUP.txt and INSTRUCTIONS.txt for usage examples.

Copyright: Copyright (C) 2016 Freewill FX Co., Ltd. All rights reserved.

'''

import subprocess
from subprocess import call
import sys
import argparse
import importlib
import time
import debug_helpers
from debug_helpers import dprint
import zipfile
import os
import shutil
import glob
import traceback


# global vars
g_process_start_time = time.time()
g_target_db_types = ['postgresql','mssql','sqlite3']
g_check_and_dont_create_if_empty = False

def parse_cmd_args():
    parser = argparse.ArgumentParser(description=infostr, usage=usagestr,
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument('--azm_file',help='''An AZENQOS Android .azm file (or directory that contains multiple .azm files)
    that contains the SQLite3 "azqdata.db" to merge/import.
    (a .azm is actually a zip file)''', required=True)
    
    parser.add_argument('--unmerge',
                        action='store_true',
                        help="un-merge mode: remove all rows of this azm from target_db.",
                        default=False)
    
    parser.add_argument('--folder_mode_stop_on_first_failure',
                        action='store_true',
                        help="""If --azm_file supplied was a folder,
                        by default it would not stop in the first failed azm.
                        Set this to make it stop at the first .azm that failed to merge/import.
                        """,
                        default=False)
    
    parser.add_argument('--target_db_type', choices=g_target_db_types,
                        help="Target DBMS type ", required=True)
    
    parser.add_argument('--target_sqlite3_file', 
                        help="Target sqlite3 file (to create) for merge", required=False)
    
    parser.add_argument('--server_url',
                        help="Target DBMS Server URL (domain or ip).",
                        required=False, default="localhost")
    
    parser.add_argument('--server_user',
                        help="Target login: username.", required=True)
    
    parser.add_argument('--server_password',
                        help="Target login: password.", required=True)
    
    parser.add_argument('--server_database',
                        help="Target database name.", required=True)
        
    
    
    parser.add_argument('--force_unmerge_logs_longer_than_24_hrs',
                        action='store_true',
                        help="""By default if log duration is > 24 hrs
                        will not be allowed to --unmerge automatically - the cases
                        might be from time change or some time bug that might
                        accidentally remove other logs' data from this imei.
                        Use this flag if you already verified/checked this
                        .azm's db date and still want to unmerge""",
                        default=False)
    
    
    
    parser.add_argument('--check_and_dont_create_if_empty',
                        action='store_true',
                        help="Force check and omit table create if table is empty. This check, however, can make processing slower than default behavior.",
                        default=False)
    
    parser.add_argument('--sqlite3_executable',
                        help="Full path to sqlite3 executable.",
                        default="sqlite3")
    
    parser.add_argument('--mssql_odbc_driver',
                        help="Driver string for SQL Server",
                        default="{SQL Server Native Client 11.0}")

    ''' now force bulk mode only - not fully tested non-bulk about atomicity - after unmerge        
    parser.add_argument('--mssql_local_bulk_insert_mode_disable',
                        action='store_true',
                        help="""Set this to disable 'BULK INSERT' mode - 
                        which is for local SQL Server only as it requires direct file access.""",
                        default=False)
                        '''
    
    parser.add_argument('--dump_to_file_mode',
                        action='store_true',
                        help="""Set this to force full dump of sqlite3 db to .sql file
                        first before reading and parsing.
                        (We found that this is a bit slower and taks some disk space).""",
                        default=False)
  
    
    args = vars(parser.parse_args())
    return args

def is_dump_schema_only_for_target_db_type(args):
    
    # now force bulk so always schema only
    return True
    
    if (args['unmerge']):
        return True
    
    if (args['target_db_type'] == 'mssql' and not args['mssql_local_bulk_insert_mode_disable'] == True):
        return True
    
    return False


def popen_sqlite3_dump(args):
    params = [
        args['sqlite3_executable'],
        args['file']        
        ]
    
    if (is_dump_schema_only_for_target_db_type(args)):
        params.append(".schema")
    else:
        params.append(".dump")
    
    dprint("popen_sqlite3_dump params: "+str(params))
    sub = subprocess.Popen(
        params,
        bufsize = -1, # -1 means default of OS. If no buf then it will block sqlite and very slow
        shell=False,
        stdout=subprocess.PIPE,
        #stderr=subprocess.STDOUT
        #stderr=sys.stdout.fileno()
        )
    dprint("subporcess popen done")
    return sub

# Dump db to a text sql file
def dump_db_to_sql(dir_processing_azm):
    dumped_sql_fp = "{}_dump.sql".format(args['file'])
    cmd = "{} \"{}\" \".out {}\" \".dump\"".format(args['sqlite3_executable'],
                                    args['file'], dumped_sql_fp.replace("\\", "\\\\"))
    print "cmd: "+cmd
    ret = call(cmd, shell=False)
    print "conv ret: "+str(ret)
    if (ret != 0):
        print "dump db to {} file failed - ABORT".format(dumped_sql_fp)
        return None
    
    print "dump db to {} file success".format(dumped_sql_fp)
    return dumped_sql_fp

# global vars for handle_sql3_dump_line
g_is_in_insert = False
g_insert_buf = ""

# global module functions
g_connect_function = None
g_check_if_already_merged_function = None
g_create_function = None
g_commit_function = None
g_close_function = None

# g_insert_function = None


# parse multi-line statements info one for insert, parse create, commit commands and call related funcs of target db type module
def handle_sql3_dump_line(args, line):
    global g_is_in_insert
    global g_insert_buf
    global g_insert_function
        
    if g_is_in_insert is True:
        g_insert_buf = g_insert_buf + line
        
        if line.strip().endswith(");"):
            
            handle_ret = g_insert_function(args, g_insert_buf.strip())
            
            g_is_in_insert = False       
            g_insert_buf = None
                        
            # dprint("multi line insert END:")            
            
            return handle_ret
        else:
            # dprint("multi line insert still not ending - continue")
            return True
        
        
    if (
            line.startswith("CREATE TABLE") and
            not line.startswith("CREATE TABLE android_metadata")
    ):
        # TODO: flush remaining buffered INSERTS?

        # get table name:
        table_name = line.split(" ")[2].replace("\"", "")
        
        print("\nprocessing: create/alter/insert for table_name: "+table_name)
        
        # check if create is required for this table (omit if empty)
        create = True        
        if (not g_check_and_dont_create_if_empty):
            pass # print "create_empty_tables specified in option - do create" # always create - flag override
        else:
            # checking can make final processing slower...
            print("checking if table is empty ...")
            sqlstr = "SELECT 1 FROM {} LIMIT 1".format(table_name)
            cmd = [args['sqlite3_executable'],args['file'],sqlstr]
            outstr = subprocess.check_output(cmd)
            # print "check has_rows out: "+outstr
            has_rows = (outstr.strip() == "1")
            # print "check has_rows ret: " + str(has_rows)
            if (has_rows):
                print "table is not empty - do create"
                create = True
            else:
                print "table is empty - omit create"
                create = False            
        
        if create:
            print "processing create at handler module..." # always create - flag override                
            handle_ret = g_create_function(args, line)
        
        
    elif (line.startswith("COMMIT;")):        
        print("\nprocessing: commit")        
        handle_ret = g_commit_function(args, line)
        return handle_ret
    elif (line.startswith("INSERT INTO")):
        
        raise Exception("ABORT: currently bulk insert mode is used so only scheme should be dumped/read... found INSERT INTO - abort")
        
        table_name = line.split(" ")[2].replace("\"", "")
        if (table_name == "android_metadata"):
            return True #omit
        
        line_stripped = line.strip() 
        if line_stripped.endswith(");"):
            # dprint("single line insert")
            handle_ret = g_insert_function(args, line_stripped)            
            return handle_ret
        else:
            # dprint("multi line insert START")
            g_is_in_insert = True
            g_insert_buf = line
            return True
    else:
        # dprint "omit line: "+line
        return True

    return False


# unzip azm file to a tmp processing folder
def unzip_azm_to_tmp_folder(args):         
    
    dprint("unzip_azm_to_tmp_folder 0")
    print "args['azm_file']: "+args['azm_file']
    azm_fp = os.path.abspath(args['azm_file'])
    print "azm_fp: "+azm_fp
    
    if os.path.isfile(azm_fp):
        pass
    else:
        raise Exception("INVALID: - azm file does not exist at given path: "+str(azm_fp)+" - ABORT")        
    
    dir_azm = os.path.dirname(azm_fp)
    print "dir_azm: "+dir_azm
    azm_name_no_ext = os.path.splitext(os.path.basename(azm_fp))[0]
    print "azm_name_no_ext: "+azm_name_no_ext
    dir_processing_azm = os.path.join(dir_azm, "tmp_process_"+azm_name_no_ext.replace(" ","-")) # replace 'space' in azm file name 
    args['dir_processing_azm'] = dir_processing_azm
    
    dprint("unzip_azm_to_tmp_folder 1")
    
    # try clear tmp processing folder just in case it exists from manual unzip or previous failed imports
    try:
        shutil.rmtree(dir_processing_azm)        
    except Exception as e:
        estr = str(e)
        if ("cannot find the path specified" in estr or "No such file or" in estr):
            pass
        else:
            print("rmtree dir_processing_azm: "+str(e))
            raise e
    
    dprint("unzip_azm_to_tmp_folder 2")
    
    os.mkdir(dir_processing_azm)
    
    dprint("unzip_azm_to_tmp_folder 3")
    
    try:
        azm = zipfile.ZipFile(args['azm_file'],'r')
        azm.extract("azqdata.db", dir_processing_azm)
        azm.close()
    except:
        raise("Invalid azm_file: azm file does not contain azqdata.db database.")
        
    
    dprint("unzip_azm_to_tmp_folder 4")
    
    args['file'] = os.path.join(dir_processing_azm, "azqdata.db")
    return dir_processing_azm


def cleanup_tmp_dir(dir_processing_azm):
    # clear tmp processing folder
    attempts = range(5) # 0 to 4 
    imax = len(attempts)
    for i in attempts:
        try:
            # print("cleaning up tmp dir...")        
            shutil.rmtree(dir_processing_azm)
            break
            # print("cleanup tmp_processing_ dir done.")
            
        except Exception as e:
            print("warning: attempt %d/%d - failed to delete tmp dir: %s - dir_processing_azm: %s" % (i, imax, e,dir_processing_azm))
            time.sleep(0.01) # sleep 10 millis
            pass


def check_azm_azq_app_version(args):
    # check version of AZENQOS app that produced the .azm file - must be at least 3.0.562    
    MIN_APP_V0 = 3
    MIN_APP_V1 = 0
    MIN_APP_V2 = 579
    sqlstr = "select log_app_version from logs" # there is always only 1 ver of AZENQOS app for 1 azm - and normally 1 row of logs per azm too - but limit just in-case to be future-proof 
    cmd = [args['sqlite3_executable'],args['file'],sqlstr]
    outstr = subprocess.check_output(cmd).strip()
    outstr = outstr.replace("v","") # replace 'v' prefix - like "v3.0.562" outstr
    parts = outstr.split(".")
    v0 = int(parts[0])
    v1 = int(parts[1])
    v2 = int(parts[2])
    if (v0 >= MIN_APP_V0 and v1 >= MIN_APP_V1 and v2 >= MIN_APP_V2):
        pass
    else:
        raise("Invalid azm_file: the azm file must be from AZENQOS apps with versions {}.{}.{} or newer.".format(MIN_APP_V0,MIN_APP_V1,MIN_APP_V2))
        

def process_azm_file(args):
    proc_start_time = time.time()
    ret = -9
    use_popen_mode = True
    
    try:
        dir_processing_azm = None
        dir_processing_azm = unzip_azm_to_tmp_folder(args)
        args['dir_processing_azm'] = dir_processing_azm
        
        check_azm_azq_app_version(args)
        
        g_check_and_dont_create_if_empty = args['check_and_dont_create_if_empty']
        use_popen_mode = not args['dump_to_file_mode']
        
        if args['target_db_type'] == "sqlite3":
            if args['target_sqlite3_file'] is None:
                raise Exception("INVALID: sqlite3 merge mode requires --target_sqlite3_file option to be specified - ABORT")            
            else:
                use_popen_mode = False # dump to .sql file for .read 
            
        if (use_popen_mode):
            print "using live in-memory pipe of sqlite3 dump output parse mode"
        else:
            print "using full dump of sqlite3 to file mode"
        
        
        dump_process = None
        dumped_sql_fp = None
        
        if (use_popen_mode):
            print("starting sqlite3 subporcess...")
            dump_process = popen_sqlite3_dump(args)
            if dump_process is None:
                raise Exception("FATAL: dump_process is None in popen_mode - ABORT")
        else:
            print("starting sqlite3 to dump db to .sql file...")
            dumped_sql_fp = dump_db_to_sql(dir_processing_azm)
            if dumped_sql_fp is None:
                raise Exception("FATAL: dumped_sql_fp is None in non popen_mode - ABORT")
        
        
        # sqlite3 merge is simple run .read on args['dumped_sql_fp']
        if args['target_db_type'] == "sqlite3":
            print "sqlite3 - import to {} from {}".format(args['target_sqlite3_file'], dumped_sql_fp)
            cmd = "{} \"{}\" \".read {}\"".format(args['sqlite3_executable'],
                                            args['target_sqlite3_file'], dumped_sql_fp.replace("\\", "\\\\"))
            print "cmd: "+cmd
            ret = call(cmd, shell=False)
            print "import ret: "+str(ret)
            if (ret == 0):
                print( "\n=== SUCCESS - import completed in %s seconds" % (time.time() - proc_start_time) )
                cleanup_tmp_dir(dir_processing_azm)
                return 0
            else:
                cleanup_tmp_dir(dir_processing_azm)
                raise("\n=== FAILED - ret %d - operation completed in %s seconds" % (ret, time.time() - proc_start_time))
                
            raise Exception("FATAL: sqlite3 mode merge process failed - invalid state")
            
        # now we use bulk insert done at create/commit funcs instead g_insert_function = getattr(mod, 'handle_sqlite3_dump_insert')
            
        print "### connecting to dbms..."    
        ret = g_connect_function(args)
        
        if ret == False:
            raise Exception("FATAL: connect_function failed")
            
        # check if this azm is already imported/merged in target db (and exit of already imported)
        # get log_ori_file_name
        sqlstr = "select log_ori_file_name from logs limit 1"
        cmd = [args['sqlite3_executable'],args['file'],sqlstr]
        outstr = subprocess.check_output(cmd)
        log_ori_file_name = outstr.strip()
        if (not ".azm" in log_ori_file_name):
            raise Exception("FATAL: Failed to get log_ori_file_name from logs table of this azm's db - ABORT.")
        
        if (args['unmerge']):
            print "### unmerge mode"
            # unmerge mode would be handled by same check_if_already_merged_function below - the 'unmerge' flag is in args
        
        g_check_if_already_merged_function(args, log_ori_file_name)
        
        ''' now we're connected and ready to import, open dumped file and hadle CREATE/INSERT
        operations for current target_type (DBMS type)'''
        
        sql_dump_file = None
        if (use_popen_mode == False):
            sql_dump_file = open(dumped_sql_fp, 'r')
        
        # output for try manual import mode
        # args['out_sql_dump_file'] = open("out_for_dbtype_{}.sql".format(args['file']), 'w')
        
        dprint("entering main loop")
        
        n_lines_parsed = 0
        while(True):
            if (use_popen_mode):
                line = dump_process.stdout.readline()        
            else:
                line = sql_dump_file.readline()
            dprint("read line: "+line)
            # when EOF is reached, we'd get an empty string
            if (line == ""):
                print "\nreached end of file/output"
                break
            else:
                n_lines_parsed = n_lines_parsed + 1
                handle_sql3_dump_line(args, line)
        
        
        # finally call commit again in case the file didn't have a 'commit' line at the end
        print "### calling handler's commit func as we've reached the end..."
        
        handle_ret = g_commit_function(args, line)
        
        # call close() for that dbms handler   
    
        operation = "merge/import"
        if (args['unmerge']):
            operation = "unmerge/delete"
            
        if (n_lines_parsed != 0):
            print( "\n=== SUCCESS - %s completed in %s seconds - tatal n_lines_parsed %d (not including bulk-inserted-table-content-lines)" % (operation, time.time() - proc_start_time, n_lines_parsed) )
            ret =  0
        else:
            
            raise("\n=== FAILED - %s - no lines parsed - tatal n_lines_parsed %d operation completed in %s seconds ===" % (operation, n_lines_parsed, time.time() - proc_start_time))
        
    
    except Exception as e:
        print "re-raise exception e"
        raise e
    finally:
        print "cleanup start..."
        if (use_popen_mode):
            # clean-up dump process
            try:
                dump_process.kill()
                dump_process.terminate()
            except:
                pass
        else:
            sql_dump_file.close()
        g_close_function(args)
        if debug_helpers.debug == 1:
            pass # keep files for analysis of exceptions in debug mode
        else:
            print "cleanup_tmp_dir..."
            cleanup_tmp_dir(dir_processing_azm)
    
    return ret

#################### Program START

print infostr

args = parse_cmd_args()

mod_name = args['target_db_type']
if  mod_name in ['postgresql','mssql']:
    mod_name = 'gen_sql'
mod_name = mod_name + "_handler"
print "### get module: ", mod_name
importlib.import_module(mod_name)
mod = sys.modules[mod_name]
print "module dir: "+str(dir(mod))

g_connect_function = getattr(mod, 'connect')
g_check_if_already_merged_function = getattr(mod, 'check_if_already_merged')
g_create_function = getattr(mod, 'create')
g_commit_function = getattr(mod, 'commit')
g_close_function = getattr(mod, 'close')
    

azm_files = []
# check if supplied 'azm_file' is a folder - then iterate over all azms in that folder
if (os.path.isdir(args['azm_file'])):
    print "supplied azm_file is a directory - get a list of .azm files to process:"
    dir = args['azm_file']
    azm_files = glob.glob(os.path.join(dir,"*.azm"))
else:
    azm_files = [args['azm_file']]
    
nazm = len(azm_files)
print "n_azm_files to process: {}".format(nazm)
print "list of azm files to process: "+str(azm_files)
iazm = 0
ifailed = 0
ret = -1
had_errors = False

for azm in azm_files:
    iazm = iazm + 1
    args['azm_file'] = azm
    print "## START process azm {}/{}: '{}'".format(iazm, nazm, azm)
    try: 
        ret = process_azm_file(args)
        if (ret != 0):
            raise Exception("ABORT: process_azm_file failed with ret code: "+str(ret))        
        print "## DONE process azm {}/{}: '{}' retcode {}".format(iazm, nazm, azm, ret)        
    except Exception as e:
        ifailed = ifailed + 1
        had_errors = True
        type_, value_, traceback_ = sys.exc_info()
        exstr = traceback.format_exception(type_, value_, traceback_)
        print "## FAILED: process azm {} failed with below exception:\n(start of exception)\n{}\n{}(end of exception)".format(azm,str(e),exstr)
        if (args['folder_mode_stop_on_first_failure']):
            print "--folder_mode_stop_on_first_failure specified - exit now."
            exit(-9)
        
if (had_errors == False):
    print "SUCCESS - operation completed successfully for all azm files (tatal: %d) - in %.03f seconds." % (iazm,  time.time() - g_process_start_time)
else:
    print "COMPLETED WITH ERRORS - operation completed but had encountered errors (tatal: %d, failed: %d) - in %.03f seconds - (use --folder_mode_stop_on_first_failure to stop on first failed azm file)." % (iazm,ifailed, time.time() - g_process_start_time)
exit(ret)