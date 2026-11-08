{
  'UBTU-22-215020' => ['systemd-timesyncd', 0.3],
  'UBTU-22-215025' => ['ntp', 0.3],
  'UBTU-22-215030' => ['rsh-server', 0.7]
}.each do |id, (name, severity)|
  control id do
    impact severity
    title "Prohibited package #{name} is absent"
    desc 'Package absence only; other implementations, service state and configuration require separate assessment.'
    tag stig_id: id
    ref 'Ubuntu 22.04 STIG V2R7 mapping', url: 'https://github.com/ComplianceAsCode/content/blob/bb706dd4dddcfc76df85712874a7e0544821cb97/controls/stig_ubuntu2204.yml'
    describe package(name) do
      it { should_not be_installed }
    end
  end
end
