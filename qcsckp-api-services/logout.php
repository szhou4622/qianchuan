<?php
declare(strict_types=1);

require_once __DIR__ . '/includes/bootstrap.php';
require_once __DIR__ . '/includes/portal_auth.php';

portal_logout();
header('Location: /');
exit;
