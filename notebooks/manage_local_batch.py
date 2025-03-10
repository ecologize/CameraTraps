"""

manage_local_batch.py
   
Semi-automated process for managing a local MegaDetector job, including
standard postprocessing steps.

This script is not intended to be run from top to bottom like a typical Python script,
it's a notebook disguised with a .py extension.  It's the Bestest Most Awesome way to
run MegaDetector, but it's also pretty subtle; if you want to play with this, you might
want to check in with cameratraps@lila.science for some tips.  Otherwise... YMMV.

Some general notes on using this script, which I run in Spyder, though everything will be
the same if you are reading this in Jupyter Notebook (using the .ipynb version of the 
script):

* Typically when I have a MegaDetector job to run, I make a copy of this script.  Let's 
  say I'm running a job for an organization called "bibblebop"; I have a big folder of
  job-specific copies of this script, and I might save a new one called "bibblebop-2023-07-26.py" 
  (the filename doesn't matter, it just helps me keep these organized).

* There are three variables you need to set in this script before you start running code:
  "input_path", "organization_name_short", and "job_date".  You will get a sensible error if you forget 
  to set any of these.  In this case I might set those to "/data/bibblebobcamerastuff",
  "bibblebop", and "2023-07-26", respectively.

* The defaults assume you want to split the job into two tasks (this is the default because I have 
  two GPUs).  Nothing bad will happen if you do this on a zero-GPU or single-GPU machine, but if you
  want everything to run in one logical task, change "n_gpus" and "n_jobs" to 1 (instead of 2).

* After setting the required variables, I run the first few cells - up to and including the one 
  called "Generate commands" - which collectively take basically zero seconds.  After you run the
  "Generate commands" cell, you will have a folder that looks something like:

    ~/postprocessing/bibblebop/bibblebop-2023-07-06-mdv5a/

  On Windows, this means:

    ~/postprocessing/bibblebop/bibblebop-2023-07-06-mdv5a/

  Everything related to this job - scripts, outputs, intermediate stuff - will be in this folder.
  Specifically, after the "Generate commands" cell, you'll have scripts in that folder called something
  like:

  run_chunk_000_gpu_00.sh (or .bat on Windows)

  Personally, I like to run that script directly in a command prompt (I just leave Spyder open, though 
  it's OK if Spyder gets shut down while MD is running).
  
  At this point, once you get the hang of it, you've invested about zero seconds of human time,
  but possibly several days of unattended compute time, depending on the size of your job.
  
* Then when the jobs are done, back to the interactive environment!  I run the next few cells,
  which make sure the job finished OK, and the cell called "Post-processing (pre-RDE)", which 
  generates an HTML preview of the results.  You are very plausibly done at this point, and can ignore
  all the remaining cells.  If you want to do things like repeat detection elimination, or running 
  a classifier, or splitting your results file up in specialized ways, there are cells for all of those
  things, but now you're in power-user territory, so I'm going to leave this guide here.  Email
  cameratraps@lila.science with questions about the fancy stuff.

"""

#%% Imports and constants

import json
import os
import stat
import time
import re    

import humanfriendly

from tqdm import tqdm
from collections import defaultdict

from megadetector.utils import path_utils
from megadetector.utils.ct_utils import split_list_into_n_chunks
from megadetector.utils.ct_utils import image_file_to_camera_folder
from megadetector.detection.run_detector_batch import \
    load_and_run_detector_batch, write_results_to_file
from megadetector.detection.run_detector import DEFAULT_OUTPUT_CONFIDENCE_THRESHOLD
from megadetector.detection.run_detector import estimate_md_images_per_second
from megadetector.postprocessing.postprocess_batch_results import \
    PostProcessingOptions, process_batch_results
from megadetector.detection.run_detector import get_detector_version_from_model_file

## Inference options

# To specify a non-default confidence threshold for including detections in the .json file
json_threshold = None

# Turn warnings into errors if more than this many images are missing
max_tolerable_failed_images = 100

# Should we supply the --image_queue_option to run_detector_batch.py?  I only set this 
# when I have a very slow drive and a comparably fast GPU.  When this is enabled, checkpointing
# is not supported within a job, so I set n_jobs to a large number (typically 100).
use_image_queue = False

# Only relevant when we're using a single GPU
default_gpu_number = 0

# Should we supply --quiet to run_detector_batch.py?
quiet_mode = True

# Specify a target image size when running MD... strongly recommended to leave this at "None"
#
# When using augmented inference, if you leave this at "None", run_inference_with_yolov5_val.py
# will use its default size, which is 1280 * 1.3, which is almost always what you want.
image_size = None

# Should we include image size, timestamp, and/or EXIF data in MD output?
include_image_size = False
include_image_timestamp = False
include_exif_data = False

# String to pass as the "detector_options" parameter to run_detector_batch (or None)
# detector_options = 'compatibility_mode=classic'
# detector_options = 'compatibility_mode=modern'
detector_options = None

# Only relevant when running on CPU
ncores = 1

# If False, we'll load chunk files with file lists if they exist
force_enumeration = False

# Prefer threads on Windows, processes on Linux
parallelization_defaults_to_threads = False

# This is for things like image rendering, not for MegaDetector
default_workers_for_parallel_tasks = 30

overwrite_handling = 'skip' # 'skip', 'error', or 'overwrite'

# The function used to get camera names from image paths, used only for repeat
# detection elimination.  This defaults to a standard function (image_file_to_camera_folder) 
# that replaces typical strings like "BTCF", "RECNYX001", or "DCIM".  There's an example near 
# the end of this notebook of using a custom function instead.
relative_path_to_location = image_file_to_camera_folder

# This will be the .json results file after RDE; if this is still None when
# we get to classification stuff, that will indicate that we didn't do RDE.
filtered_output_filename = None


# OS-specific script line continuation character (modified later if we're running on Windows)
slcc = '\\'

# OS-specific script comment character (modified later if we're running on Windows)
scc = '#' 

# OS-specific script extension (modified later if we're running on Windows)
script_extension = '.sh'

# Stuff we stick into scripts to ensure early termination if there's an error
script_header = '#!/bin/bash\n\nset -e\n'

# Include this after each command in a .sh/.bat file
command_suffix = ''

if os.name == 'nt':
    
    script_header = ''
    slcc = '^'
    scc = 'REM'
    script_extension = '.bat'

    command_suffix = 'if %errorlevel% neq 0 exit /b %errorlevel%\n'
    
    # My experience has been that Python multiprocessing is flaky on Windows, so 
    # default to threads on Windows
    parallelization_defaults_to_threads = True
    default_workers_for_parallel_tasks = 10


## Constants related to using YOLOv5's val.py

# Should we use YOLOv5's val.py instead of run_detector_batch.py?
use_yolo_inference_scripts = False

# Directory in which to run val.py (relevant for YOLOv5, not for YOLOv8)
yolo_working_dir = os.path.expanduser('~/git/yolov5')

# Only used for loading the mapping from class indices to names
yolo_dataset_file = None

# 'yolov5' or 'yolov8'; assumes YOLOv5 if this is None
yolo_model_type = None

# Inference batch size
yolo_batch_size = 1

# Should we remove intermediate files used for running YOLOv5's val.py?
#
# Only relevant if use_yolo_inference_scripts is True.
remove_yolo_intermediate_results = True
remove_yolo_symlink_folder = True
use_symlinks_for_yolo_inference = True
write_yolo_debug_output = False

# Should we apply YOLOv5's test-time augmentation?
augment = False


## Constants related to tiled inference

use_tiled_inference = False

# Should we delete tiles after each job?  Only set this to False for debugging;
# large jobs will take up a lot of space if you keep tiles around after each task.
remove_tiles = True
tile_size = (1280,1280)
tile_overlap = 0.2


#%% Constants I set per script

input_path = '/drive/organization'

assert not (input_path.endswith('/') or input_path.endswith('\\'))
assert os.path.isdir(input_path), 'Could not find input folder {}'.format(input_path)
input_path = input_path.replace('\\','/')

organization_name_short = 'organization'
job_date = None # '2025-01-01'
assert job_date is not None and organization_name_short != 'organization'

# Optional descriptor
job_tag = None

if job_tag is None:
    job_description_string = ''
else:
    job_description_string = '-' + job_tag

model_file = 'MDV5A' # 'MDV5A', 'MDV5B', 'MDV4'

postprocessing_base = os.path.expanduser('~/postprocessing')

# Number of jobs to split data into, typically equal to the number of available GPUs, though
# when using augmentation or an image queue (and thus not using checkpoints), I typically
# use ~100 jobs per GPU; those serve as de facto checkpoints.
n_jobs = 2
n_gpus = 2

# Set to "None" when using augmentation or an image queue, which don't currently support
# checkpointing.  Don't worry, this will be assert()'d in the next cell.
checkpoint_frequency = 10000

# Estimate inference speed for the current GPU
approx_images_per_second = estimate_md_images_per_second(model_file) 
    
# Rough estimate for the inference time cost of augmentation    
if augment and (approx_images_per_second is not None):
    approx_images_per_second = approx_images_per_second * 0.7
    
base_task_name = organization_name_short + '-' + job_date + job_description_string + '-' + \
    get_detector_version_from_model_file(model_file)
base_output_folder_name = os.path.join(postprocessing_base,organization_name_short)
os.makedirs(base_output_folder_name,exist_ok=True)


#%% Derived variables, constant validation, path setup

if use_image_queue:
    assert checkpoint_frequency is None,\
        'Checkpointing is not supported when using an image queue'        
    
if augment:
    assert checkpoint_frequency is None,\
        'Checkpointing is not supported when using augmentation'
    
    assert use_yolo_inference_scripts,\
        'Augmentation is only supported when running with the YOLO inference scripts'

if use_tiled_inference:
    assert not augment, \
        'Augmentation is not supported when using tiled inference'
    assert not use_yolo_inference_scripts, \
        'Using the YOLO inference script is not supported when using tiled inference'
    assert checkpoint_frequency is None, \
        'Checkpointing is not supported when using tiled inference'
        
filename_base = os.path.join(base_output_folder_name, base_task_name)
combined_api_output_folder = os.path.join(filename_base, 'combined_api_outputs')
postprocessing_output_folder = os.path.join(filename_base, 'preview')

combined_api_output_file = os.path.join(
    combined_api_output_folder,
    '{}_detections.json'.format(base_task_name))

os.makedirs(filename_base, exist_ok=True)
os.makedirs(combined_api_output_folder, exist_ok=True)
os.makedirs(postprocessing_output_folder, exist_ok=True)

if input_path.endswith('/'):
    input_path = input_path[0:-1]

print('Output folder:\n{}'.format(filename_base))


#%% Enumerate files

# Have we already listed files for this job?
chunk_files = os.listdir(filename_base)
pattern = re.compile('chunk\d+.json')
chunk_files = [fn for fn in chunk_files if pattern.match(fn)]

if (not force_enumeration) and (len(chunk_files) > 0):
    
    print('Found {} chunk files in folder {}, bypassing enumeration'.format(
        len(chunk_files),
        filename_base))
    
    all_images = []
    for fn in chunk_files:
        with open(os.path.join(filename_base,fn),'r') as f:
            chunk = json.load(f)
            assert isinstance(chunk,list)
            all_images.extend(chunk)
    all_images = sorted(all_images)
    
    print('Loaded {} image files from {} chunks in {}'.format(
        len(all_images),len(chunk_files),filename_base))

else:

    print('Enumerating image files in {}'.format(input_path))
    
    all_images = sorted(path_utils.find_images(input_path,recursive=True,convert_slashes=True))
    
    # It's common to run this notebook on an external drive with the main folders in the drive root
    all_images = [fn for fn in all_images if not \
                  (fn.startswith('$RECYCLE') or fn.startswith('System Volume Information'))]
        
    print('')
        
    print('Enumerated {} image files in {}'.format(len(all_images),input_path))
        

#%% Divide images into chunks 

folder_chunks = split_list_into_n_chunks(all_images,n_jobs)


#%% Estimate total time

if approx_images_per_second is None:
    
    print("Can't estimate inference time for the current environment")
    
else:
        
    n_images = len(all_images)
    execution_seconds = n_images / approx_images_per_second
    wallclock_seconds = execution_seconds / n_gpus
    print('Expected time: {}'.format(humanfriendly.format_timespan(wallclock_seconds)))
    
    seconds_per_chunk = len(folder_chunks[0]) / approx_images_per_second
    print('Expected time per chunk: {}'.format(humanfriendly.format_timespan(seconds_per_chunk)))


#%% Write file lists

task_info = []

for i_chunk,chunk_list in enumerate(folder_chunks):
    
    chunk_fn = os.path.join(filename_base,'chunk{}.json'.format(str(i_chunk).zfill(3)))
    task_info.append({'id':i_chunk,'input_file':chunk_fn})
    path_utils.write_list_to_file(chunk_fn, chunk_list)
    
    
#%% Generate commands

# A list of the scripts tied to each GPU, as absolute paths.  We'll write this out at
# the end so each GPU's list of commands can be run at once
gpu_to_scripts = defaultdict(list)

# i_task = 0; task = task_info[i_task]
for i_task,task in enumerate(task_info):
    
    chunk_file = task['input_file']
    checkpoint_filename = chunk_file.replace('.json','_checkpoint.json')
    
    output_fn = chunk_file.replace('.json','_results.json')
    
    task['output_file'] = output_fn
    
    if n_gpus > 1:
        gpu_number = i_task % n_gpus        
    else:
        gpu_number = default_gpu_number
        
    image_size_string = ''
    if image_size is not None:
        image_size_string = '--image_size {}'.format(image_size)
        
    # Generate the script to run MD
    
    if use_yolo_inference_scripts:

        augment_string = ''
        if augment:
            augment_string = '--augment_enabled 1'
        else:
            augment_string = '--augment_enabled 0'
        
        batch_string = '--batch_size {}'.format(yolo_batch_size)
        
        symlink_folder = os.path.join(filename_base,'symlinks','symlinks_{}'.format(
            str(i_task).zfill(3)))
        yolo_results_folder = os.path.join(filename_base,'yolo_results','yolo_results_{}'.format(
            str(i_task).zfill(3)))
                
        symlink_folder_string = '--symlink_folder "{}"'.format(symlink_folder)
        yolo_results_folder_string = '--yolo_results_folder "{}"'.format(yolo_results_folder)
        
        remove_symlink_folder_string = ''
        if not remove_yolo_symlink_folder:
            remove_symlink_folder_string = '--no_remove_symlink_folder'
        
        write_yolo_debug_output_string = ''
        if write_yolo_debug_output:
            write_yolo_debug_output = '--write_yolo_debug_output'
            
        remove_yolo_results_string = ''
        if not remove_yolo_intermediate_results:
            remove_yolo_results_string = '--no_remove_yolo_results_folder'
        
        confidence_threshold_string = ''
        if json_threshold is not None:
            confidence_threshold_string = '--conf_thres {}'.format(json_threshold)
        else:
            confidence_threshold_string = '--conf_thres {}'.format(DEFAULT_OUTPUT_CONFIDENCE_THRESHOLD)
            
        cmd = ''
        
        device_string = '--device {}'.format(gpu_number)
        
        overwrite_handling_string = '--overwrite_handling {}'.format(overwrite_handling)        
        
        cmd += f'python run_inference_with_yolov5_val.py "{model_file}" "{chunk_file}" "{output_fn}" '
        cmd += f'{image_size_string} {augment_string} '
        cmd += f'{symlink_folder_string} {yolo_results_folder_string} {remove_yolo_results_string} '
        cmd += f'{remove_symlink_folder_string} {confidence_threshold_string} {device_string} '
        cmd += f'{overwrite_handling_string} {batch_string} {write_yolo_debug_output_string}'
                
        if yolo_working_dir is not None:
            cmd += f' --yolo_working_folder "{yolo_working_dir}"'
        if yolo_dataset_file is not None:
            cmd += ' --yolo_dataset_file "{}"'.format(yolo_dataset_file)
        if yolo_model_type is not None:
            cmd += ' --model_type {}'.format(yolo_model_type)
            
        if not use_symlinks_for_yolo_inference:
            cmd += ' --no_use_symlinks'
        
        cmd += '\n'
    
    elif use_tiled_inference:
        
        tiling_folder = os.path.join(filename_base,'tile_cache','tile_cache_{}'.format(
            str(i_task).zfill(3)))
        
        if os.name == 'nt':
            cuda_string = f'set CUDA_VISIBLE_DEVICES={gpu_number} & '
        else:
            cuda_string = f'CUDA_VISIBLE_DEVICES={gpu_number} '
                        
        cmd = f'{cuda_string} python run_tiled_inference.py "{model_file}" "{input_path}" "{tiling_folder}" "{output_fn}"'
        
        cmd += f' --image_list "{chunk_file}"'
        cmd += f' --overwrite_handling {overwrite_handling}'
        
        if not remove_tiles:
            cmd += ' --no_remove_tiles'
            
        # If we're using non-default tile sizes
        if tile_size is not None and (tile_size[0] > 0 or tile_size[1] > 0):            
            cmd += ' --tile_size_x {} --tile_size_y {}'.format(tile_size[0],tile_size[1])
            
        if tile_overlap is not None:
            cmd += f' --tile_overlap {tile_overlap}'            
        
    else:
        
        if os.name == 'nt':
            cuda_string = f'set CUDA_VISIBLE_DEVICES={gpu_number} & '
        else:
            cuda_string = f'CUDA_VISIBLE_DEVICES={gpu_number} '
                
        checkpoint_frequency_string = ''
        checkpoint_path_string = ''
        
        if checkpoint_frequency is not None and checkpoint_frequency > 0:
            checkpoint_frequency_string = f'--checkpoint_frequency {checkpoint_frequency}'
            checkpoint_path_string = '--checkpoint_path "{}"'.format(checkpoint_filename)
                
        use_image_queue_string = ''
        if (use_image_queue):
            use_image_queue_string = '--use_image_queue'

        ncores_string = ''
        if (ncores > 1):
            ncores_string = '--ncores {}'.format(ncores)
            
        quiet_string = ''
        if quiet_mode:
            quiet_string = '--quiet'
        
        confidence_threshold_string = ''
        if json_threshold is not None:
            confidence_threshold_string = '--threshold {}'.format(json_threshold)
        
        overwrite_handling_string = '--overwrite_handling {}'.format(overwrite_handling)        
        cmd = f'{cuda_string} python run_detector_batch.py "{model_file}" "{chunk_file}" "{output_fn}" {checkpoint_frequency_string} {checkpoint_path_string} {use_image_queue_string} {ncores_string} {quiet_string} {image_size_string} {confidence_threshold_string} {overwrite_handling_string}'
        
        if include_image_size:
            cmd += ' --include_image_size'
        if include_image_timestamp:
            cmd += ' --include_image_timestamp'
        if include_exif_data:
            cmd += ' --include_exif_data'
        
        if detector_options is not None:
            cmd += ' --detector_options "{}"'.format(detector_options)
            
    cmd_file = os.path.join(filename_base,'run_chunk_{}_gpu_{}{}'.format(str(i_task).zfill(3),
                            str(gpu_number).zfill(2),script_extension))
    
    with open(cmd_file,'w') as f:
        if script_header is not None and len(script_header) > 0:
            f.write(script_header + '\n')
        f.write(cmd + '\n')
    
    st = os.stat(cmd_file)
    os.chmod(cmd_file, st.st_mode | stat.S_IEXEC)
        
    task['command'] = cmd
    task['command_file'] = cmd_file

    # Generate the script to resume from the checkpoint (only supported with MD inference code)
    
    gpu_to_scripts[gpu_number].append(cmd_file)
    
    if checkpoint_frequency is not None:
        
        resume_string = ' --resume_from_checkpoint "{}"'.format(checkpoint_filename)
        resume_cmd = cmd + resume_string
    
        resume_cmd_file = os.path.join(filename_base,
                                       'resume_chunk_{}_gpu_{}{}'.format(str(i_task).zfill(3),
                                       str(gpu_number).zfill(2),script_extension))
        
        with open(resume_cmd_file,'w') as f:
            if script_header is not None and len(script_header) > 0:
                f.write(script_header + '\n')
            f.write(resume_cmd + '\n')
        
        st = os.stat(resume_cmd_file)
        os.chmod(resume_cmd_file, st.st_mode | stat.S_IEXEC)
        
        task['resume_command'] = resume_cmd
        task['resume_command_file'] = resume_cmd_file

# ...for each task

# Write out a script for each GPU that runs all of the commands associated with
# that GPU.  Typically only used when running lots of little scripts in lieu
# of checkpointing.
for gpu_number in gpu_to_scripts:
    
    gpu_script_file = os.path.join(filename_base,'run_all_for_gpu_{}{}'.format(
        str(gpu_number).zfill(2),script_extension))
    with open(gpu_script_file,'w') as f:
        if script_header is not None and len(script_header) > 0:
            f.write(script_header + '\n')
        for script_name in gpu_to_scripts[gpu_number]:
            s = script_name
            # When calling a series of batch files on Windows from within a batch file, you need to
            # use "call", or only the first will be executed.  No, it doesn't make sense.
            if os.name == 'nt':
                s = 'call ' + s
            f.write(s + '\n')
        f.write('echo "Finished all commands for GPU {}"'.format(gpu_number))
    st = os.stat(gpu_script_file)
    os.chmod(gpu_script_file, st.st_mode | stat.S_IEXEC)

# ...for each GPU


#%% Run the tasks

r"""
tl;dr: I almost never run this cell.

Long version...

The cells we've run so far wrote out some shell scripts (.bat files on Windows, 
.sh files on Linx/Mac) that will run MegaDetector.  I like to leave the interactive
environment at this point and run those scripts at the command line.  So, for example,
if you're on Windows, and you've basically used the default values above, there will be
batch files called, e.g.:

c:\users\[username]\postprocessing\[organization]\[job_name]\run_chunk_000_gpu_00.bat
c:\users\[username]\postprocessing\[organization]\[job_name]\run_chunk_001_gpu_01.bat

Those batch files expect to be run from the "detection" folder of the MegaDetector repo,
typically:
    
c:\git\MegaDetector\megadetector\detection

All of that said, you don't *have* to do this at the command line.  The following cell 
runs these scripts programmatically, so if you set "run_tasks_in_notebook" to "True"
and run this cell, you can run MegaDetector without leaving this notebook.

One downside of the programmatic approach is that this cell doesn't yet parallelize over
multiple processes, so the tasks will run serially.  This only matters if you have 
multiple GPUs.
"""

run_tasks_in_notebook = False

if run_tasks_in_notebook:
    
    assert not use_yolo_inference_scripts, \
        'If you want to use the YOLOv5 inference scripts, you can\'t run the model interactively (yet)'
        
    # i_task = 0; task = task_info[i_task]
    for i_task,task in enumerate(task_info):
    
        chunk_file = task['input_file']
        output_fn = task['output_file']
        
        checkpoint_filename = chunk_file.replace('.json','_checkpoint.json')
        
        if json_threshold is not None:
            confidence_threshold = json_threshold
        else:
            confidence_threshold = DEFAULT_OUTPUT_CONFIDENCE_THRESHOLD
            
        if checkpoint_frequency is not None and checkpoint_frequency > 0:
            cp_freq_arg = checkpoint_frequency
        else:
            cp_freq_arg = -1
            
        start_time = time.time()
        results = load_and_run_detector_batch(model_file=model_file, 
                                              image_file_names=chunk_file, 
                                              checkpoint_path=checkpoint_filename, 
                                              confidence_threshold=confidence_threshold,
                                              checkpoint_frequency=cp_freq_arg, 
                                              results=None,
                                              n_cores=ncores, 
                                              use_image_queue=use_image_queue,
                                              quiet=quiet_mode,
                                              image_size=image_size)        
        elapsed = time.time() - start_time
        
        print('Task {}: finished inference for {} images in {}'.format(
            i_task, len(results),humanfriendly.format_timespan(elapsed)))

        # This will write absolute paths to the file, we'll fix this later
        write_results_to_file(results, output_fn, detector_file=model_file)

        if checkpoint_frequency is not None and checkpoint_frequency > 0:
            if os.path.isfile(checkpoint_filename):                
                os.remove(checkpoint_filename)
                print('Deleted checkpoint file {}'.format(checkpoint_filename))
                
    # ...for each chunk
    
# ...if we're running tasks in this notebook

    
#%% Load results, look for failed or missing images in each task

# Check that all task output files exist

missing_output_files = []

# i_task = 0; task = task_info[i_task]
for i_task,task in tqdm(enumerate(task_info),total=len(task_info)):    
    output_file = task['output_file']
    if not os.path.isfile(output_file):
        missing_output_files.append(output_file)

if len(missing_output_files) > 0:
    print('Missing {} output files:'.format(len(missing_output_files)))
    for s in missing_output_files:
        print(s)
    raise Exception('Missing output files')

n_total_failures = 0

# i_task = 0; task = task_info[i_task]
for i_task,task in tqdm(enumerate(task_info),total=len(task_info)):
    
    chunk_file = task['input_file']
    output_file = task['output_file']
    
    with open(chunk_file,'r') as f:
        task_images = json.load(f)
    with open(output_file,'r') as f:
        task_results = json.load(f)
    
    task_images_set = set(task_images)
    filename_to_results = {}
    
    n_task_failures = 0
    
    # im = task_results['images'][0]
    for im in task_results['images']:
        
        # Most of the time, inference result files use absolute paths, but it's 
        # getting annoying to make sure that's *always* true, so handle both here.  
        # E.g., when using tiled inference, paths will be relative.
        if not os.path.isabs(im['file']):
            fn = os.path.join(input_path,im['file']).replace('\\','/')
            im['file'] = fn
        assert im['file'].startswith(input_path)
        assert im['file'] in task_images_set
        filename_to_results[im['file']] = im
        if 'failure' in im:
            assert im['failure'] is not None
            n_task_failures += 1
    
    task['n_failures'] = n_task_failures
    task['results'] = task_results
    
    for fn in task_images:
        assert fn in filename_to_results, \
            'File {} not found in results for task {}'.format(fn,i_task)
    
    n_total_failures += n_task_failures

# ...for each task

assert n_total_failures < max_tolerable_failed_images,\
    '{} failures (max tolerable set to {})'.format(n_total_failures,
                                                   max_tolerable_failed_images)

print('Processed all {} images with {} failures'.format(
    len(all_images),n_total_failures))
        

##%% Merge results files and make filenames relative

combined_results = {}
combined_results['images'] = []
images_processed = set()

for i_task,task in tqdm(enumerate(task_info),total=len(task_info)):

    task_results = task['results']
    
    if i_task == 0:
        combined_results['info'] = task_results['info']
        combined_results['detection_categories'] = task_results['detection_categories']        
    else:
        assert task_results['info']['format_version'] == combined_results['info']['format_version']
        assert task_results['detection_categories'] == combined_results['detection_categories']
        
    # Make sure we didn't see this image in another chunk
    for im in task_results['images']:
        assert im['file'] not in images_processed
        images_processed.add(im['file'])

    combined_results['images'].extend(task_results['images'])
    
# Check that we ended up with the right number of images    
assert len(combined_results['images']) == len(all_images), \
    'Expected {} images in combined results, found {}'.format(
        len(all_images),len(combined_results['images']))

# Check uniqueness
result_filenames = [im['file'] for im in combined_results['images']]
assert len(combined_results['images']) == len(set(result_filenames))

# Convert to relative paths, preserving '/' as the path separator, regardless of OS
for im in combined_results['images']:
    assert '\\' not in im['file']
    assert im['file'].startswith(input_path)
    if input_path.endswith(':'):
        im['file'] = im['file'].replace(input_path,'',1)
    else:
        im['file'] = im['file'].replace(input_path + '/','',1)
    
with open(combined_api_output_file,'w') as f:
    json.dump(combined_results,f,indent=1)

print('Wrote results to {}'.format(combined_api_output_file))


#%% Post-processing (pre-RDE)

"""
NB: I almost never run this cell.  This preview the results *before* repeat detection
elimination (RDE), but since I'm essentially always doing RDE, I'm basically never 
interested in this preview.  There is a similar cell below for previewing results 
*after* RDE, which I almost always run.
"""

render_animals_only = False

options = PostProcessingOptions()
options.image_base_dir = input_path
options.include_almost_detections = True
options.num_images_to_sample = 7500
options.confidence_threshold = 0.2
options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
options.ground_truth_json_file = None
options.separate_detections_by_category = True
options.sample_seed = 0
options.max_figures_per_html_file = 2500
options.sort_classification_results_by_count = True

options.parallelize_rendering = True
options.parallelize_rendering_n_cores = default_workers_for_parallel_tasks
options.parallelize_rendering_with_threads = parallelization_defaults_to_threads

if render_animals_only:
    # Omit some pages from the output, useful when animals are rare
    options.rendering_bypass_sets = ['detections_person','detections_vehicle',
                                     'detections_person_vehicle','non_detections']

output_base = os.path.join(postprocessing_output_folder,
    base_task_name + '_{:.3f}'.format(options.confidence_threshold))
if render_animals_only:
    output_base = output_base + '_animals_only'

os.makedirs(output_base, exist_ok=True)
print('Processing to {}'.format(output_base))

options.md_results_file = combined_api_output_file
options.output_dir = output_base
ppresults = process_batch_results(options)
html_output_file = ppresults.output_html_file
path_utils.open_file(html_output_file,attempt_to_open_in_wsl_host=True,browser_name='chrome')
# import clipboard; clipboard.copy(html_output_file)


#%% Repeat detection elimination, phase 1

from megadetector.postprocessing.repeat_detection_elimination import repeat_detections_core

task_index = 0

options = repeat_detections_core.RepeatDetectionOptions()

options.confidenceMin = 0.1
options.confidenceMax = 1.01
options.iouThreshold = 0.85
options.occurrenceThreshold = 15
options.maxSuspiciousDetectionSize = 0.2
# options.minSuspiciousDetectionSize = 0.05

options.parallelizationUsesThreads = parallelization_defaults_to_threads
options.nWorkers = default_workers_for_parallel_tasks

# This will cause a very light gray box to get drawn around all the detections
# we're *not* considering as suspicious.
options.bRenderOtherDetections = True
options.otherDetectionsThreshold = options.confidenceMin

options.bRenderDetectionTiles = True
options.maxOutputImageWidth = 2000
options.detectionTilesMaxCrops = 100

# options.lineThickness = 5
# options.boxExpansion = 8

options.customDirNameFunction = relative_path_to_location

# To invoke custom collapsing of folders for a particular naming scheme
# options.customDirNameFunction = custom_relative_path_to_location

options.bRenderHtml = False
options.imageBase = input_path
rde_string = 'rde_{:.3f}_{:.3f}_{}_{:.3f}'.format(
    options.confidenceMin, options.iouThreshold,
    options.occurrenceThreshold, options.maxSuspiciousDetectionSize)
options.outputBase = os.path.join(filename_base, rde_string + '_task_{}'.format(task_index))
options.filenameReplacements = None # {'':''}

# Exclude people and vehicles from RDE
# options.excludeClasses = [2,3]

# options.maxImagesPerFolder = 50000
# options.includeFolders = ['a/b/c','d/e/f']
# options.excludeFolders = ['a/b/c','d/e/f']

options.debugMaxDir = -1
options.debugMaxRenderDir = -1
options.debugMaxRenderDetection = -1
options.debugMaxRenderInstance = -1

# Can be None, 'xsort', or 'clustersort'
options.smartSort = 'xsort'

suspicious_detection_results = repeat_detections_core.find_repeat_detections(combined_api_output_file,
                                                                             outputFilename=None,
                                                                             options=options)


#%% Manual RDE step

## DELETE THE VALID DETECTIONS ##

# If you run this line, it will open the folder up in your file browser
path_utils.open_file(os.path.dirname(suspicious_detection_results.filterFile),
                     attempt_to_open_in_wsl_host=True)

#
# If you ran the previous cell, but then you change your mind and you don't want to do 
# the RDE step, that's fine, but don't just blast through this cell once you've run the 
# previous cell.  If you do that, you're implicitly telling the notebook that you looked 
# at everything in that folder, and confirmed there were no red boxes on animals.
#
# Instead, either change "filtered_output_filename" below to "combined_api_output_file", 
# or delete *all* the images in the filtering folder.
#


#%% Re-filtering

from megadetector.postprocessing.repeat_detection_elimination import remove_repeat_detections

filtered_output_filename = path_utils.insert_before_extension(combined_api_output_file, 
                                                              'filtered_{}'.format(rde_string))

remove_repeat_detections.remove_repeat_detections(
    inputFile=combined_api_output_file,
    outputFile=filtered_output_filename,
    filteringDir=os.path.dirname(suspicious_detection_results.filterFile)
    )


#%% Post-processing (post-RDE)

render_animals_only = False

options = PostProcessingOptions()
options.image_base_dir = input_path
options.include_almost_detections = True
options.num_images_to_sample = 7500
options.confidence_threshold = 0.2
options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
options.ground_truth_json_file = None
options.separate_detections_by_category = True
options.sample_seed = 0
options.max_figures_per_html_file = 2500
options.sort_classification_results_by_count = True

options.parallelize_rendering = True
options.parallelize_rendering_n_cores = default_workers_for_parallel_tasks
options.parallelize_rendering_with_threads = parallelization_defaults_to_threads

if render_animals_only:
    # Omit some pages from the output, useful when animals are rare
    options.rendering_bypass_sets = ['detections_person','detections_vehicle',
                                      'detections_person_vehicle','non_detections']    

output_base = os.path.join(postprocessing_output_folder, 
    base_task_name + '_{}_{:.3f}'.format(rde_string, options.confidence_threshold))    

if render_animals_only:
    output_base = output_base + '_render_animals_only'
os.makedirs(output_base, exist_ok=True)

print('Processing post-RDE to {}'.format(output_base))

options.md_results_file = filtered_output_filename
options.output_dir = output_base
ppresults = process_batch_results(options)
html_output_file = ppresults.output_html_file

path_utils.open_file(html_output_file,attempt_to_open_in_wsl_host=True,browser_name='chrome')
# import clipboard; clipboard.copy(html_output_file)


#%% Run MegaClassifier (actually, write out a script that runs MegaClassifier)

# Variables that will indicate which classifiers we ran
final_output_path_mc = None
final_output_path_ic = None

# If we didn't do RDE
if filtered_output_filename is None:
    print("Warning: it looks like you didn't do RDE, using the raw output file")
    filtered_output_filename = combined_api_output_file
    
classifier_name_short = 'megaclassifier'
threshold_str = '0.15'
classifier_name = 'megaclassifier_v0.1_efficientnet-b3'

organization_name = organization_name_short
job_name = base_task_name
input_filename = filtered_output_filename # combined_api_output_file
input_files = [input_filename]
image_base = input_path
crop_path = os.path.join(os.path.expanduser('~/crops'),job_name + '_crops')
output_base = combined_api_output_folder
device_id = 0

output_file = os.path.join(filename_base,'run_{}_'.format(classifier_name_short) + job_name + script_extension)

classifier_base = os.path.expanduser('~/models/camera_traps/megaclassifier/v0.1/')
assert os.path.isdir(classifier_base)

checkpoint_path = os.path.join(classifier_base,'megaclassifier_v0.1_efficientnet-b3_compiled.pt')
assert os.path.isfile(checkpoint_path)

classifier_categories_path = os.path.join(classifier_base,'megaclassifier_v0.1_index_to_name.json')
assert os.path.isfile(classifier_categories_path)

target_mapping_path = os.path.join(classifier_base,'idfg_to_megaclassifier_labels.json')
assert os.path.isfile(target_mapping_path)

classifier_output_suffix = '_megaclassifier_output.csv.gz'
final_output_suffix = '_megaclassifier.json'

n_threads_str = str(default_workers_for_parallel_tasks)
image_size_str = '300'
batch_size_str = '64'
num_workers_str = str(default_workers_for_parallel_tasks)
classification_threshold_str = '0.05'

logdir = filename_base

# This is just passed along to the metadata in the output file, it has no impact
# on how the classification scripts run.
typical_classification_threshold_str = '0.75'


##%% Set up environment

commands = []
# commands.append('cd MegaDetector/megadetector/classification\n')
# commands.append('mamba activate cameratraps-classifier\n')

if script_header is not None and len(script_header) > 0:
    commands.append(script_header)


##%% Crop images

commands.append('\n' + scc + ' Cropping ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    crop_cmd = ''
    
    crop_comment = '\n' + scc + ' Cropping {}\n\n'.format(fn)
    crop_cmd += crop_comment
    
    crop_cmd += "python crop_detections.py " + slcc + "\n" + \
    	 ' "' + input_file_path + '" ' + slcc + '\n' + \
         ' "' + crop_path + '" ' + slcc + '\n' + \
         ' ' + '--images-dir "' + image_base + '"' + ' ' + slcc + '\n' + \
         ' ' + '--threshold "' + threshold_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--square-crops ' + ' ' + slcc + '\n' + \
         ' ' + '--threads "' + n_threads_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--logdir "' + logdir + '"' + '\n' + \
         ' ' + '\n'
    crop_cmd = '{}'.format(crop_cmd)
    commands.append(crop_cmd)

    if len(command_suffix) > 0:
        commands.append(command_suffix)
    
    
##%% Run classifier

commands.append('\n' + scc + ' Classifying ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    classifier_output_path = crop_path + classifier_output_suffix
    
    classify_cmd = ''
    
    classify_comment = '\n' + scc + ' Classifying {}\n\n'.format(fn)
    classify_cmd += classify_comment
    
    classify_cmd += "python run_classifier.py " + slcc + "\n" + \
    	 ' "' + checkpoint_path + '" ' + slcc + '\n' + \
         ' "' + crop_path + '" ' + slcc + '\n' + \
         ' "' + classifier_output_path + '" ' + slcc + '\n' + \
         ' ' + '--detections-json "' + input_file_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--classifier-categories "' + classifier_categories_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--image-size "' + image_size_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--batch-size "' + batch_size_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--num-workers "' + num_workers_str + '"' + ' ' + slcc + '\n'
    
    if device_id is not None:
        classify_cmd += ' ' + '--device {}'.format(device_id)
        
    classify_cmd += '\n\n'        
    classify_cmd = '{}'.format(classify_cmd)
    commands.append(classify_cmd)
    
    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Remap classifier outputs

commands.append('\n' + scc + ' Remapping ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    classifier_output_path = crop_path + classifier_output_suffix
    classifier_output_path_remapped = \
        classifier_output_path.replace(".csv.gz","_remapped.csv.gz")
    assert not (classifier_output_path == classifier_output_path_remapped)
    
    output_label_index = classifier_output_path_remapped.replace(
        "_remapped.csv.gz","_label_index_remapped.json")
                                       
    remap_cmd = ''
    
    remap_comment = '\n' + scc + ' Remapping {}\n\n'.format(fn)
    remap_cmd += remap_comment
    
    remap_cmd += "python aggregate_classifier_probs.py " + slcc + "\n" + \
        ' "' + classifier_output_path + '" ' + slcc + '\n' + \
        ' ' + '--target-mapping "' + target_mapping_path + '"' + ' ' + slcc + '\n' + \
        ' ' + '--output-csv "' + classifier_output_path_remapped + '"' + ' ' + slcc + '\n' + \
        ' ' + '--output-label-index "' + output_label_index + '"' \
        '\n'
     
    remap_cmd = '{}'.format(remap_cmd)
    commands.append(remap_cmd)
    
    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Merge classification and detection outputs

commands.append('\n\n' + scc + ' Merging ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    classifier_output_path = crop_path + classifier_output_suffix
    
    classifier_output_path_remapped = \
        classifier_output_path.replace(".csv.gz","_remapped.csv.gz")
    
    output_label_index = classifier_output_path_remapped.replace(
        "_remapped.csv.gz","_label_index_remapped.json")
    
    final_output_path = os.path.join(output_base,
                                     os.path.basename(classifier_output_path)).\
        replace(classifier_output_suffix,
        final_output_suffix)
    final_output_path = final_output_path.replace('_detections','')
    final_output_path = final_output_path.replace('_crops','')
    final_output_path_mc = final_output_path
    
    merge_cmd = ''
    
    merge_comment = '\n' + scc + ' Merging {}\n\n'.format(fn)
    merge_cmd += merge_comment
    
    merge_cmd += "python merge_classification_detection_output.py " + slcc + "\n" + \
    	 ' "' + classifier_output_path_remapped + '" ' + slcc + '\n' + \
         ' "' + output_label_index + '" ' + slcc + '\n' + \
         ' ' + '--output-json "' + final_output_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--detection-json "' + input_file_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--classifier-name "' + classifier_name + '"' + ' ' + slcc + '\n' + \
         ' ' + '--threshold "' + classification_threshold_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--typical-confidence-threshold "' + typical_classification_threshold_str + '"' + '\n' + \
         '\n'
    merge_cmd = '{}'.format(merge_cmd)
    commands.append(merge_cmd)

    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Write out classification script

with open(output_file,'w') as f:
    for s in commands:
        f.write('{}'.format(s))

st = os.stat(output_file)
os.chmod(output_file, st.st_mode | stat.S_IEXEC)


#%% Run a non-MegaClassifier classifier (i.e., a classifier with no output mapping)

classifier_name_short = 'idfgclassifier'
threshold_str = '0.15' # 0.6
classifier_name = 'idfg_classifier_ckpt_14_compiled'

organization_name = organization_name_short
job_name = base_task_name
input_filename = filtered_output_filename # combined_api_output_file
input_files = [input_filename]
image_base = input_path
crop_path = os.path.join(os.path.expanduser('~/crops'),job_name + '_crops')
output_base = combined_api_output_folder
device_id = 1

output_file = os.path.join(filename_base,'run_{}_'.format(classifier_name_short) + job_name +  script_extension)

classifier_base = os.path.expanduser('~/models/camera_traps/idfg_classifier/idfg_classifier_20200905_042558')
assert os.path.isdir(classifier_base)

checkpoint_path = os.path.join(classifier_base,'idfg_classifier_ckpt_14_compiled.pt')
assert os.path.isfile(checkpoint_path)

classifier_categories_path = os.path.join(classifier_base,'label_index.json')
assert os.path.isfile(classifier_categories_path)

classifier_output_suffix = '_{}_output.csv.gz'.format(classifier_name_short)
final_output_suffix = '_{}.json'.format(classifier_name_short)

threshold_str = '0.65'
n_threads_str = str(default_workers_for_parallel_tasks)
image_size_str = '300'
batch_size_str = '64'
num_workers_str = str(default_workers_for_parallel_tasks)
logdir = filename_base

classification_threshold_str = '0.05'

# This is just passed along to the metadata in the output file, it has no impact
# on how the classification scripts run.
typical_classification_threshold_str = '0.75'


##%% Set up environment

commands = []
if script_header is not None and len(script_header) > 0:
    commands.append(script_header)


##%% Crop images
    
commands.append('\n' + scc + ' Cropping ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    crop_cmd = ''
    
    crop_comment = '\n' + scc + ' Cropping {}\n'.format(fn)
    crop_cmd += crop_comment
    
    crop_cmd += "python crop_detections.py " + slcc + "\n" + \
    	 ' "' + input_file_path + '" ' + slcc + '\n' + \
         ' "' + crop_path + '" ' + slcc + '\n' + \
         ' ' + '--images-dir "' + image_base + '"' + ' ' + slcc + '\n' + \
         ' ' + '--threshold "' + threshold_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--square-crops ' + ' ' + slcc + '\n' + \
         ' ' + '--threads "' + n_threads_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--logdir "' + logdir + '"' + '\n' + \
         '\n'
    crop_cmd = '{}'.format(crop_cmd)
    commands.append(crop_cmd)

    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Run classifier

commands.append('\n' + scc + ' Classifying ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    classifier_output_path = crop_path + classifier_output_suffix
    
    classify_cmd = ''
    
    classify_comment = '\n' + scc + ' Classifying {}\n'.format(fn)
    classify_cmd += classify_comment
    
    classify_cmd += "python run_classifier.py " + slcc + "\n" + \
    	 ' "' + checkpoint_path + '" ' + slcc + '\n' + \
         ' "' + crop_path + '" ' + slcc + '\n' + \
         ' "' + classifier_output_path + '" ' + slcc + '\n' + \
         ' ' + '--detections-json "' + input_file_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--classifier-categories "' + classifier_categories_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--image-size "' + image_size_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--batch-size "' + batch_size_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--num-workers "' + num_workers_str + '"' + ' ' + slcc + '\n'
    
    if device_id is not None:
        classify_cmd += ' ' + '--device {}'.format(device_id)
        
    classify_cmd += '\n\n'    
    classify_cmd = '{}'.format(classify_cmd)
    commands.append(classify_cmd)
		
    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Merge classification and detection outputs

commands.append('\n' + scc + ' Merging ' + scc + '\n')

# fn = input_files[0]
for fn in input_files:

    input_file_path = fn
    classifier_output_path = crop_path + classifier_output_suffix
    final_output_path = os.path.join(output_base,
                                     os.path.basename(classifier_output_path)).\
                                     replace(classifier_output_suffix,
                                     final_output_suffix)
    final_output_path = final_output_path.replace('_detections','')
    final_output_path = final_output_path.replace('_crops','')
    final_output_path_ic = final_output_path
    
    merge_cmd = ''
    
    merge_comment = '\n' + scc + ' Merging {}\n'.format(fn)
    merge_cmd += merge_comment
    
    merge_cmd += "python merge_classification_detection_output.py " + slcc + "\n" + \
    	 ' "' + classifier_output_path + '" ' + slcc + '\n' + \
         ' "' + classifier_categories_path + '" ' + slcc + '\n' + \
         ' ' + '--output-json "' + final_output_path_ic + '"' + ' ' + slcc + '\n' + \
         ' ' + '--detection-json "' + input_file_path + '"' + ' ' + slcc + '\n' + \
         ' ' + '--classifier-name "' + classifier_name + '"' + ' ' + slcc + '\n' + \
         ' ' + '--threshold "' + classification_threshold_str + '"' + ' ' + slcc + '\n' + \
         ' ' + '--typical-confidence-threshold "' + typical_classification_threshold_str + '"' + '\n' + \
         '\n'
    merge_cmd = '{}'.format(merge_cmd)
    commands.append(merge_cmd)

    if len(command_suffix) > 0:
        commands.append(command_suffix)


##%% Write everything out

with open(output_file,'w') as f:
    for s in commands:
        f.write('{}'.format(s))

import stat
st = os.stat(output_file)
os.chmod(output_file, st.st_mode | stat.S_IEXEC)


#%% Run the classifier(s) via the .sh script(s) or batch file(s) we just wrote

# I do this manually, primarily because this requires a different mamba environment
# (cameratraps-classifier) from MegaDetector's environment (cameratraps-detector).
#
# The next few pseudo-cells (#%) in this script are basically always run all at once, getting us
# all the way from running the classifier to classification previews and zipped .json files that
# are ready to upload.


##%% Do All The Rest of The Stuff

# The remaining cells require no human intervention, so although a few conceptually-unrelated things 
# happen (e.g., sequence-level smoothing, preview generation, and zipping all the .json files), I group these 
# all into one super-cell.  Every click counts.


##%% Within-image classification smoothing

classification_detection_files = []

from megadetector.postprocessing.classification_postprocessing import \
    smooth_classification_results_image_level

# Did we run MegaClassifier?
if final_output_path_mc is not None:
    classification_detection_files.append(final_output_path_mc)
    
# Did we run the IDFG classifier?
if final_output_path_ic is not None:
    classification_detection_files.append(final_output_path_ic)

assert all([os.path.isfile(fn) for fn in classification_detection_files])

smoothed_classification_files = []

for final_output_path in classification_detection_files:

    classifier_output_path = final_output_path
    classifier_output_path_within_image_smoothing = classifier_output_path.replace(
        '.json','_within_image_smoothing.json')    
    smoothed_classification_files.append(classifier_output_path_within_image_smoothing)
    smooth_classification_results_image_level(input_file=classifier_output_path,
                                              output_file=classifier_output_path_within_image_smoothing,
                                              options=None)

# ...for each file we want to smooth


##%% Read EXIF date and time from all images

from megadetector.data_management import read_exif
exif_options = read_exif.ReadExifOptions()

exif_options.verbose = False
exif_options.n_workers = default_workers_for_parallel_tasks
exif_options.use_threads = parallelization_defaults_to_threads
exif_options.processing_library = 'pil'
exif_options.byte_handling = 'delete'
exif_options.tags_to_include = ['DateTime','DateTimeOriginal']

exif_results_file = os.path.join(filename_base,'exif_data.json')

if os.path.isfile(exif_results_file):
    print('Reading EXIF results from {}'.format(exif_results_file))
    with open(exif_results_file,'r') as f:
        exif_results = json.load(f)
else:        
    exif_results = read_exif.read_exif_from_folder(input_path,
                                                   output_file=exif_results_file,
                                                   options=exif_options)


##%% Prepare COCO-camera-traps-compatible image objects for EXIF results

# ...and add location/datetime info based on filenames and EXIF information.

from megadetector.data_management.read_exif import \
    exif_results_to_cct, ExifResultsToCCTOptions
from megadetector.utils.ct_utils import is_function_name

exif_results_to_cct_options = ExifResultsToCCTOptions()

# If we've defined a "custom_relative_path_to_location" location, which by convention
# is what we use in this notebook for a non-standard location mapping function, use it 
# to parse locations when creating the CCT data.
if is_function_name('custom_relative_path_to_location',locals()):
    print('Using custom location mapping function in EXIF conversion')
    exif_results_to_cct_options.filename_to_location_function = \
        custom_relative_path_to_location # noqa
        
cct_dict = exif_results_to_cct(exif_results=exif_results,
                               cct_output_file=None,
                               options=exif_results_to_cct_options)


##%% Assemble images into sequences

from megadetector.data_management import cct_json_utils

print('Assembling images into sequences')
cct_json_utils.create_sequences(cct_dict)


##%% Sequence-level smoothing

from megadetector.postprocessing.classification_postprocessing import \
    ClassificationSmoothingOptionsSequenceLevel, smooth_classification_results_sequence_level

options = ClassificationSmoothingOptionsSequenceLevel()
options.category_names_to_smooth_to = set(['deer','elk','cow','canid','cat','bird','bear'])
options.min_dominant_class_ratio_for_secondary_override_table = {'cow':2,None:3}

sequence_level_smoothing_input_file = smoothed_classification_files[0]
sequence_smoothed_classification_file = sequence_level_smoothing_input_file.replace(
    '.json','_seqsmoothing.json')

_ = smooth_classification_results_sequence_level(md_results=sequence_level_smoothing_input_file,
                                             cct_sequence_information=cct_dict,
                                             output_file=sequence_smoothed_classification_file,
                                             options=options)


##%% Post-processing (post-classification, post-within-image-and-within-sequence-smoothing)

options = PostProcessingOptions()
options.image_base_dir = input_path
options.include_almost_detections = True
options.num_images_to_sample = 10000
options.confidence_threshold = 0.2
options.classification_confidence_threshold = 0.7
options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
options.ground_truth_json_file = None
options.separate_detections_by_category = True
options.max_figures_per_html_file = 2500
options.sort_classification_results_by_count = True

options.parallelize_rendering = True
options.parallelize_rendering_n_cores = default_workers_for_parallel_tasks
options.parallelize_rendering_with_threads = parallelization_defaults_to_threads

folder_token = sequence_smoothed_classification_file.split(os.path.sep)[-1].replace(
    '_within_image_smoothing_seqsmoothing','')
folder_token = folder_token.replace('.json','_seqsmoothing')

output_base = os.path.join(postprocessing_output_folder, folder_token + \
    base_task_name + '_{:.3f}'.format(options.confidence_threshold))
os.makedirs(output_base, exist_ok=True)
print('Processing {} to {}'.format(base_task_name, output_base))

options.md_results_file = sequence_smoothed_classification_file
options.output_dir = output_base
ppresults = process_batch_results(options)
path_utils.open_file(ppresults.output_html_file,attempt_to_open_in_wsl_host=True,browser_name='chrome')
# import clipboard; clipboard.copy(ppresults.output_html_file)


##%% Zip .json files

from megadetector.utils.path_utils import parallel_zip_files

json_files = os.listdir(combined_api_output_folder)
json_files = [fn for fn in json_files if fn.endswith('.json')]
json_files = [os.path.join(combined_api_output_folder,fn) for fn in json_files]

parallel_zip_files(json_files)


#%% 99.9% of jobs end here

# Everything after this is run ad hoc and/or requires some manual editing.


#%% Compare results files for different model versions (or before/after RDE)

import itertools

from megadetector.postprocessing.compare_batch_results import \
    BatchComparisonOptions, PairwiseBatchComparisonOptions, compare_batch_results

options = BatchComparisonOptions()

options.job_name = organization_name_short
options.output_folder = os.path.join(postprocessing_output_folder,'model_comparison')
options.image_folder = input_path

options.pairwise_options = []

filenames = [
    '/postprocessing/organization/mdv4_results.json',
    '/postprocessing/organization/mdv5a_results.json',
    '/postprocessing/organization/mdv5b_results.json'    
    ]

detection_thresholds = [0.7,0.15,0.15]

assert len(detection_thresholds) == len(filenames)

rendering_thresholds = [(x*0.6666) for x in detection_thresholds]

# Choose all pairwise combinations of the files in [filenames]
for i, j in itertools.combinations(list(range(0,len(filenames))),2):
        
    pairwise_options = PairwiseBatchComparisonOptions()
    
    pairwise_options.results_filename_a = filenames[i]
    pairwise_options.results_filename_b = filenames[j]
    
    pairwise_options.rendering_confidence_threshold_a = rendering_thresholds[i]
    pairwise_options.rendering_confidence_threshold_b = rendering_thresholds[j]
    
    pairwise_options.detection_thresholds_a = {'animal':detection_thresholds[i],
                                               'person':detection_thresholds[i],
                                               'vehicle':detection_thresholds[i]}
    pairwise_options.detection_thresholds_b = {'animal':detection_thresholds[j],
                                               'person':detection_thresholds[j],
                                               'vehicle':detection_thresholds[j]}
    options.pairwise_options.append(pairwise_options)

results = compare_batch_results(options)

from megadetector.utils.path_utils import open_file
open_file(results.html_output_file,attempt_to_open_in_wsl_host=True,browser_name='chrome')


#%% Merge in high-confidence detections from another results file

from megadetector.postprocessing.merge_detections import \
    MergeDetectionsOptions,merge_detections

source_files = ['']
target_file = ''
output_file = target_file.replace('.json','_merged.json')

options = MergeDetectionsOptions()
options.max_detection_size = 1.0
options.target_confidence_threshold = 0.25
options.categories_to_include = [1]
options.source_confidence_thresholds = [0.2]
merge_detections(source_files, target_file, output_file, options)

merged_detections_file = output_file


#%% Create a new category for large boxes

from megadetector.postprocessing import categorize_detections_by_size

size_options = categorize_detections_by_size.SizeCategorizationOptions()

size_options.size_thresholds = [0.9]
size_options.size_category_names = ['large_detections']

size_options.categories_to_separate = [1]
size_options.measurement = 'size' # 'width'

threshold_string = '-'.join([str(x) for x in size_options.size_thresholds])

input_file = filtered_output_filename
size_separated_file = input_file.replace('.json','-size-separated-{}.json'.format(
    threshold_string))
d = categorize_detections_by_size.categorize_detections_by_size(input_file,size_separated_file,
                                                                size_options)


#%% Preview large boxes

output_base_large_boxes = os.path.join(postprocessing_output_folder, 
    base_task_name + '_{}_{:.3f}_size_separated_boxes'.format(rde_string, options.confidence_threshold))    
os.makedirs(output_base_large_boxes, exist_ok=True)
print('Processing post-RDE, post-size-separation to {}'.format(output_base_large_boxes))

options.md_results_file = size_separated_file
options.output_dir = output_base_large_boxes

ppresults = process_batch_results(options)
html_output_file = ppresults.output_html_file
path_utils.open_file(html_output_file,attempt_to_open_in_wsl_host=True,browser_name='chrome')


#%% .json splitting

data = None

from megadetector.postprocessing.subset_json_detector_output import \
    subset_json_detector_output, SubsetJsonDetectorOutputOptions

input_filename = filtered_output_filename
output_base = os.path.join(combined_api_output_folder,base_task_name + '_json_subsets')

print('Processing file {} to {}'.format(input_filename,output_base))          

options = SubsetJsonDetectorOutputOptions()
# options.query = None
# options.replacement = None

options.split_folders = True
options.make_folder_relative = True

# Reminder: 'n_from_bottom' with a parameter of zero is the same as 'bottom'
options.split_folder_mode = 'bottom'  # 'top', 'n_from_top', 'n_from_bottom'
options.split_folder_param = 0
options.overwrite_json_files = False
options.confidence_threshold = 0.01

subset_data = subset_json_detector_output(input_filename, output_base, options, data)

# Zip the subsets folder
from megadetector.utils.path_utils import zip_folder
zip_folder(output_base,verbose=True)


#%% Custom splitting/subsetting

data = None

from megadetector.postprocessing.subset_json_detector_output import \
    subset_json_detector_output, SubsetJsonDetectorOutputOptions

input_filename = filtered_output_filename
output_base = os.path.join(filename_base,'json_subsets')

folders = os.listdir(input_path)

if data is None:
    with open(input_filename) as f:
        data = json.load(f)

print('Data set contains {} images'.format(len(data['images'])))

# i_folder = 0; folder_name = folders[i_folder]
for i_folder, folder_name in enumerate(folders):

    output_filename = os.path.join(output_base, folder_name + '.json')
    print('Processing folder {} of {} ({}) to {}'.format(i_folder, len(folders), folder_name,
          output_filename))

    options = SubsetJsonDetectorOutputOptions()
    options.confidence_threshold = 0.01
    options.overwrite_json_files = True
    options.query = folder_name + '/'

    # This doesn't do anything in this case, since we're not splitting folders
    # options.make_folder_relative = True        
    
    subset_data = subset_json_detector_output(input_filename, output_filename, options, data)


#%% String replacement
    
data = None

from megadetector.postprocessing.subset_json_detector_output import \
    subset_json_detector_output, SubsetJsonDetectorOutputOptions

input_filename = filtered_output_filename
output_filename = input_filename.replace('.json','_replaced.json')

options = SubsetJsonDetectorOutputOptions()
options.query = folder_name + '/'
options.replacement = ''
subset_json_detector_output(input_filename,output_filename,options)


#%% Splitting images into folders

from megadetector.postprocessing.separate_detections_into_folders import \
    separate_detections_into_folders, SeparateDetectionsIntoFoldersOptions

default_threshold = 0.2
base_output_folder = os.path.expanduser('~/data/{}-{}-separated'.format(base_task_name,default_threshold))

options = SeparateDetectionsIntoFoldersOptions(default_threshold)

options.results_file = filtered_output_filename
options.base_input_folder = input_path
options.base_output_folder = os.path.join(base_output_folder,folder_name)
options.n_threads = default_workers_for_parallel_tasks
options.allow_existing_directory = False

separate_detections_into_folders(options)


#%% Convert frame-level results to video-level results

# This cell is only useful if the files submitted to this job were generated via
# video_folder_to_frames().

from megadetector.detection.video_utils import frame_results_to_video_results

video_output_filename = filtered_output_filename.replace('.json','_aggregated.json')
frame_results_to_video_results(filtered_output_filename,video_output_filename)


#%% Sample custom path replacement function

def custom_relative_path_to_location(relative_path):
    
    relative_path = relative_path.replace('\\','/')    
    tokens = relative_path.split('/')
    
    # This example uses a hypothetical (but relatively common) scheme
    # where the first two slash-separated tokens define a site, e.g.
    # where filenames might look like:
    #
    # north_fork/site001/recnyx001/image001.jpg
    location_name = '/'.join(tokens[0:2])
    return location_name


#%% Test relative_path_to_location on the current dataset

with open(combined_api_output_file,'r') as f:
    d = json.load(f)
image_filenames = [im['file'] for im in d['images']]

location_names = set()

# relative_path = image_filenames[0]
for relative_path in tqdm(image_filenames):
    
    # Use the standard replacement function
    location_name = relative_path_to_location(relative_path)
    
    # Use a custom replacement function
    # location_name = custom_relative_path_to_location(relative_path)
    
    location_names.add(location_name)
    
location_names = list(location_names)
location_names.sort()

for s in location_names:
    print(s)


#%% End notebook: turn this script into a notebook (how meta!)

import os # noqa
import nbformat as nbf

if os.name == 'nt':
    git_base = r'c:\git'
else:
    git_base = os.path.expanduser('~/git')
    
input_py_file = git_base + '/MegaDetector/notebooks/manage_local_batch.py'
assert os.path.isfile(input_py_file)
output_ipynb_file = input_py_file.replace('.py','.ipynb')

nb_header = '# Managing a local MegaDetector batch'

nb_header += '\n'

nb_header += \
"""
This notebook represents an interactive process for running MegaDetector on large batches of images, including typical and optional postprocessing steps.  Everything after "Merge results..." is basically optional, and we typically do a mix of these optional steps, depending on the job.

This notebook is auto-generated from manage_local_batch.py (a cell-delimited .py file that is used the same way, typically in Spyder or VS Code).

"""

with open(input_py_file,'r') as f:
    lines = f.readlines()

header_comment = ''

assert lines[0].strip() == '"""'
assert lines[1].strip() == ''
assert lines[2].strip() == 'manage_local_batch.py'
assert lines[3].strip() == ''

i_line = 4

# Everything before the first cell is the header comment
while(not lines[i_line].startswith('#%%')):
    
    s_raw = lines[i_line]
    s_trimmed = s_raw.strip()
    
    # Ignore the closing quotes at the end of the header
    if (s_trimmed == '"""'):
        i_line += 1
        continue
    
    if len(s_trimmed) == 0:
        header_comment += '\n\n'
    else:
        header_comment += ' ' + s_raw
    i_line += 1

nb_header += header_comment
nb = nbf.v4.new_notebook()
nb['cells'].append(nbf.v4.new_markdown_cell(nb_header))

current_cell = []

def write_code_cell(c):
    
    first_non_empty_line = None
    last_non_empty_line = None
    
    for i_code_line,code_line in enumerate(c):
        if len(code_line.strip()) > 0:
            if first_non_empty_line is None:
                first_non_empty_line = i_code_line
            last_non_empty_line = i_code_line
            
    # Remove the first [first_non_empty_lines] from the list
    c = c[first_non_empty_line:]
    last_non_empty_line -= first_non_empty_line
    c = c[:last_non_empty_line+1]
    
    nb['cells'].append(nbf.v4.new_code_cell('\n'.join(c)))
        
while(True):    
            
    line = lines[i_line].rstrip()
    
    if 'end notebook' in line.lower():
        break
    
    if lines[i_line].startswith('#%% '):
        if len(current_cell) > 0:
            write_code_cell(current_cell)
            current_cell = []
        markdown_content = line.replace('#%%','##')
        nb['cells'].append(nbf.v4.new_markdown_cell(markdown_content))
    else:
        current_cell.append(line)

    i_line += 1

# Add the last cell
write_code_cell(current_cell)

nbf.write(nb,output_ipynb_file)
