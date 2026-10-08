import select
import sys
import termios
import tty

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy

FASTSAM_TOPIC = '/fastsam_node/prompt'
PROMPT_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                        durability=DurabilityPolicy.TRANSIENT_LOCAL)


class PromptCLI(Node):
    def __init__(self):
        super().__init__('prompt_cli')
        self.prompt_pub = self.create_publisher(String, FASTSAM_TOPIC, PROMPT_QOS)
        self.get_logger().info(f"=== PromptCLI for FastSAM publishing on {FASTSAM_TOPIC} ===")
        self.get_logger().info("    - Press 't' to write a prompt (comma-separated terms).")
        self.get_logger().info("    - Press 'c' to segment all objects in the scene.")
        self.get_logger().info("    - Press 'q' to quit.")

    def publish_prompt(self, prompt: str):
        msg = String()
        msg.data = prompt
        self.prompt_pub.publish(msg)
        self.get_logger().info(f"Published prompt: {prompt if prompt else '(segment everything)'}")

    def run_keyboard(self):
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while rclpy.ok():
                rclpy.spin_once(self, timeout_sec=0.0)
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not ready:
                    continue
                key = sys.stdin.read(1).lower()
                if key == 't':
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                    try:
                        self.publish_prompt(input('Enter your prompt (comma-separated if multiple): ').strip())
                    except EOFError:
                        return
                    tty.setcbreak(fd)
                elif key == 'c':
                    self.publish_prompt('')
                elif key == 'q':
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def run_lines(self):
        # Piped stdin (scripts, future VLA tests): one prompt per line.
        for line in sys.stdin:
            self.publish_prompt(line.strip())
            rclpy.spin_once(self, timeout_sec=0.0)
        # The latched sample dies with the process, so give FastSAM time to discover and receive it.
        for _ in range(20):
            if self.prompt_pub.get_subscription_count() > 0:
                break
            rclpy.spin_once(self, timeout_sec=0.1)
        rclpy.spin_once(self, timeout_sec=0.5)

def main(args=None):
    rclpy.init(args=args)
    node = PromptCLI()

    try:
        if sys.stdin.isatty():
            node.run_keyboard()
        else:
            node.run_lines()
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info("PromptCLI is shutting down.")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()