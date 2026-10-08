from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration

def generate_launch_description():
    # 1. Declare launch arguments
    # 1.1 FastSAM node arguments
    arg_fastsam_model = DeclareLaunchArgument('model', default_value='FastSAM-x.pt',
                                              description='FastSAM model file (two options: FastSAM-x.pt (default) or FastSAM-s.pt)')
    arg_image_size = DeclareLaunchArgument('img_size', default_value='640',
                                             description='Image size for FastSAM input')
    arg_confidence = DeclareLaunchArgument('conf', default_value='0.7',
                                           description='Confidence threshold for FastSAM predictions')
    arg_iou = DeclareLaunchArgument('iou', default_value='0.85',
                                    description='IOU threshold for FastSAM predictions')
    arg_prompt = DeclareLaunchArgument('prompt', default_value='',
                                      description='Text prompt for FastSAM segmentation to detect specific objects')
    arg_freeze_plane = DeclareLaunchArgument('freeze_plane', default_value='false',
                                             description='Reuses the first valid plane')
    arg_show_window = DeclareLaunchArgument('show_window', default_value='false',
                                            description='OpenCV debug window')
    
    # 1.2 SuperDec node arguments
    arg_canonical = DeclareLaunchArgument('canonical', default_value='true',
                                               description='Enable canonical mode for superdec_node')
    arg_denoise = DeclareLaunchArgument('denoise', default_value='true',
                                       description='Enable denoise for superdec_node')
    arg_uniform = DeclareLaunchArgument('uniform', default_value='true',
                                        description='Enable uniform mode for superdec_node')
    arg_complete = DeclareLaunchArgument('complete', default_value='true',
                                         description='Enable complete mode for superdec_node')
    arg_merge = DeclareLaunchArgument('merge', default_value='true',
                                       description='Enable merge mode for superdec_node')
    arg_merge_tolerance = DeclareLaunchArgument('merge_tol', default_value='1.15',
                                           description='Merge tolerance for superdec_node')
    arg_superdec_verbose = DeclareLaunchArgument('verbose', default_value='false',
                                                 description='Enable verbose output for superdec_node')
    arg_merge_grid = DeclareLaunchArgument('merge_grid', default_value='5',
                                           description='Merge grid size for superdec_node')
    arg_merge_cap = DeclareLaunchArgument('merge_cap', default_value='192',
                                          description='Grid merge exponent')
    arg_resolution = DeclareLaunchArgument('resolution', default_value='12',
                                             description='Marker mesh resolution for superquadrics visualization')
    arg_rate = DeclareLaunchArgument('rate', default_value='1.0',
                                      description='Timer rate for superdec_node (Hz)')
    arg_min_points = DeclareLaunchArgument('min_points', default_value='50',
                                           description='Minimum number of points for building superquadrics')
    arg_max_instances = DeclareLaunchArgument('max_instances', default_value='8',
                                               description='Maximum number of superquadrics instances to build')
    
    # 1.3 Common arguments
    arg_device = DeclareLaunchArgument('device', default_value='',
                                        description='Device to use for computation (e.g., cpu or cuda)')
    
    # 2. Get argument values
    fastsam_model = LaunchConfiguration('model')
    image_size = LaunchConfiguration('img_size')
    confidence = LaunchConfiguration('conf')
    iou = LaunchConfiguration('iou')
    prompt = LaunchConfiguration('prompt')
    freeze_plane = LaunchConfiguration('freeze_plane')
    show_window = LaunchConfiguration('show_window')

    superdec_verbose = LaunchConfiguration('verbose')
    canonical = LaunchConfiguration('canonical')
    denoise = LaunchConfiguration('denoise')
    uniform = LaunchConfiguration('uniform')
    merge = LaunchConfiguration('merge')
    complete = LaunchConfiguration('complete')
    merge_tolerance = LaunchConfiguration('merge_tol')
    merge_grid = LaunchConfiguration('merge_grid')
    merge_cap = LaunchConfiguration('merge_cap')
    resolution = LaunchConfiguration('resolution')
    rate = LaunchConfiguration('rate')
    min_points = LaunchConfiguration('min_points')
    max_instances = LaunchConfiguration('max_instances')

    device = LaunchConfiguration('device')

    # 3. Declare nodes and assign parameters to them
    fastsam_node = Node(
            package='intel_realsense',
            executable='fastsam_node',
            name='fastsam_node',
            output='screen',
            parameters=[{'model': fastsam_model, 
                         'img_size': image_size, 
                         'conf': confidence, 
                         'iou': iou, 
                         'device': device, 
                         'prompt': prompt, 
                         'freeze_plane': freeze_plane, 
                         'show_window': show_window}])
    
    superdec_node = Node(
            package='intel_realsense',
            executable='superdec_node',
            name='superdec_node',
            output='screen',
            parameters=[{'verbose': superdec_verbose, 
                         'device': device, 
                         'canonical': canonical, 
                         'denoise': denoise, 
                         'uniform': uniform, 
                         'complete': complete,
                         'merge': merge, 
                         'merge_tol': merge_tolerance,
                         'merge_grid': merge_grid,
                         'merge_cap': merge_cap,
                         'resolution': resolution,
                         'rate': rate,
                         'min_points': min_points,
                         'max_instances': max_instances}])
    
    # 4. Return the launch description with all declared arguments and nodes
    return LaunchDescription([
        arg_fastsam_model,
        arg_image_size,
        arg_confidence,
        arg_iou,
        arg_prompt,
        arg_freeze_plane,
        arg_show_window,
        arg_canonical,
        arg_denoise,
        arg_superdec_verbose,
        arg_uniform,
        arg_complete,
        arg_merge,
        arg_merge_tolerance,
        arg_merge_grid,
        arg_merge_cap,
        arg_resolution,
        arg_rate,
        arg_min_points,
        arg_max_instances,
        arg_device,

        fastsam_node,
        superdec_node,
    ])